import cgi
import io
import json
import mimetypes
import os
import socket
import sys
import threading
import uuid
import zipfile
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


# 普通 Python 运行时，数据和网页都位于项目目录；打包成 exe 后，网页资源
# 位于 PyInstaller 临时资源目录，而 data 必须固定保存在 exe 旁边，确保重启后不丢失。
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
    RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))
else:
    ROOT = Path(__file__).resolve().parent
    RESOURCE_ROOT = ROOT
DATA_DIR = ROOT / "data"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
DB_FILE = DATA_DIR / "app.json"
INDEX_FILE = RESOURCE_ROOT / "index.html"
PORT = 8765
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
LOCK = threading.RLock()


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def default_db():
    return {
        "tasks": [],
        "knowledge": [],
        "settings": {"reminder_time": "08:40", "local_notifications": True},
    }


def load_db():
    with LOCK:
        if not DB_FILE.exists():
            value = default_db()
            save_db(value)
            return value
        try:
            value = json.loads(DB_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = default_db()
        value.setdefault("tasks", [])
        value.setdefault("knowledge", [])
        value.setdefault("settings", {})
        value["settings"].setdefault("reminder_time", "08:40")
        value["settings"].setdefault("local_notifications", True)
        if migrate_legacy_attachments(value):
            save_db(value)
        return value


def save_db(value):
    DATA_DIR.mkdir(exist_ok=True)
    ATTACHMENTS_DIR.mkdir(exist_ok=True)
    temp = DB_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(DB_FILE)


def migrate_legacy_attachments(value):
    changed = False
    for task in value.get("tasks", []):
        legacy = task.get("attachments", [])
        if not legacy:
            continue
        records = task.setdefault("records", [])
        for attachment in legacy:
            records.insert(0, {
                "id": str(uuid.uuid4()),
                "content": f"历史附件：{attachment.get('name', '未命名文件')}",
                "source": "个人记录",
                "created_at": attachment.get("created_at", now_iso()),
                "attachments": [attachment],
            })
        task["attachments"] = []
        changed = True
    return changed


def find_task(db, task_id):
    return next((task for task in db["tasks"] if task["id"] == task_id), None)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalWorkPlanner/1.0"

    def log_message(self, format_string, *args):
        return

    def send_bytes(self, body, content_type="application/json; charset=utf-8", status=HTTPStatus.OK, download_name=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, value, status=HTTPStatus.OK):
        self.send_bytes(json_bytes(value), status=status)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2 * 1024 * 1024:
            raise ValueError("请求内容过大")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8") or "{}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            self.send_bytes(INDEX_FILE.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            self.send_json(load_db())
            return
        if path == "/api/network":
            host = lan_ip()
            self.send_json({"available": host != "127.0.0.1", "url": f"http://{host}:{PORT}"})
            return
        if path == "/api/backup":
            self.send_backup()
            return
        if path.startswith("/files/"):
            filename = os.path.basename(path.removeprefix("/files/"))
            file_path = ATTACHMENTS_DIR / filename
            if not file_path.exists() or not file_path.is_file():
                self.send_json({"error": "文件不存在"}, HTTPStatus.NOT_FOUND)
                return
            content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            self.send_bytes(file_path.read_bytes(), content_type)
            return
        self.send_json({"error": "未找到"}, HTTPStatus.NOT_FOUND)

    def send_backup(self):
        db = load_db()
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("app.json", json_bytes(db))
            if ATTACHMENTS_DIR.exists():
                for item in ATTACHMENTS_DIR.iterdir():
                    if item.is_file():
                        archive.write(item, f"attachments/{item.name}")
        filename = f"work-planner-backup-{datetime.now():%Y%m%d-%H%M}.zip"
        self.send_bytes(stream.getvalue(), "application/zip", download_name=filename)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/tasks":
                payload = self.read_json()
                task = {
                    "id": str(uuid.uuid4()),
                    "title": str(payload.get("title", "")).strip() or "未命名任务",
                    "status": payload.get("status", "进行中"),
                    "priority": payload.get("priority", "中"),
                    "due_date": payload.get("due_date", ""),
                    "due_time": payload.get("due_time", ""),
                    "description": payload.get("description", ""),
                    "reminder_time": payload.get("reminder_time", ""),
                    "created_at": now_iso(),
                    "completed_at": "",
                    "records": [],
                    "attachments": [],
                }
                with LOCK:
                    db = load_db()
                    db["tasks"].insert(0, task)
                    save_db(db)
                self.send_json(task, HTTPStatus.CREATED)
                return
            if path.startswith("/api/tasks/") and path.endswith("/records"):
                task_id = path.split("/")[3]
                if "multipart/form-data" in self.headers.get("Content-Type", ""):
                    self.handle_record_upload(task_id)
                    return
                payload = self.read_json()
                record = {
                    "id": str(uuid.uuid4()),
                    "content": str(payload.get("content", "")).strip(),
                    "source": payload.get("source", "个人记录"),
                    "created_at": now_iso(),
                    "attachments": [],
                }
                with LOCK:
                    db = load_db()
                    task = find_task(db, task_id)
                    if not task:
                        self.send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                        return
                    task.setdefault("records", []).insert(0, record)
                    save_db(db)
                self.send_json(record, HTTPStatus.CREATED)
                return
            if path.startswith("/api/tasks/") and path.endswith("/attachments"):
                self.handle_upload(path.split("/")[3])
                return
            if path == "/api/knowledge":
                payload = self.read_json()
                entry = {
                    "id": str(uuid.uuid4()),
                    "title": str(payload.get("title", "")).strip() or "未命名经验",
                    "content": payload.get("content", ""),
                    "tags": payload.get("tags", ""),
                    "source_task_id": payload.get("source_task_id", ""),
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                }
                with LOCK:
                    db = load_db()
                    db["knowledge"].insert(0, entry)
                    save_db(db)
                self.send_json(entry, HTTPStatus.CREATED)
                return
            self.send_json({"error": "未找到"}, HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc) or "请求格式错误"}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_json({"error": f"服务器错误：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_record_upload(self, task_id):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            self.send_json({"error": "截图大小需在 50MB 以内"}, HTTPStatus.BAD_REQUEST)
            return
        content_type = self.headers.get("Content-Type", "")
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type})
        content = str(form.getfirst("content", "")).strip()
        source = str(form.getfirst("source", "个人记录"))
        if not content:
            self.send_json({"error": "记录内容不能为空"}, HTTPStatus.BAD_REQUEST)
            return
        attachments = []
        image_field = form["image"] if "image" in form else None
        if image_field is not None and getattr(image_field, "filename", ""):
            original_name = os.path.basename(image_field.filename)
            mime = image_field.type or mimetypes.guess_type(original_name)[0] or "application/octet-stream"
            if not mime.startswith("image/"):
                self.send_json({"error": "这里仅支持上传截图或图片"}, HTTPStatus.BAD_REQUEST)
                return
            suffix = Path(original_name).suffix[:12]
            stored_name = f"{uuid.uuid4().hex}{suffix}"
            target = ATTACHMENTS_DIR / stored_name
            data = image_field.file.read()
            target.write_bytes(data)
            attachments.append({"id": str(uuid.uuid4()), "name": original_name, "stored_name": stored_name, "mime": mime, "size": len(data), "created_at": now_iso()})
        record = {"id": str(uuid.uuid4()), "content": content, "source": source, "created_at": now_iso(), "attachments": attachments}
        with LOCK:
            db = load_db()
            task = find_task(db, task_id)
            if not task:
                for attachment in attachments:
                    (ATTACHMENTS_DIR / attachment["stored_name"]).unlink(missing_ok=True)
                self.send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            task.setdefault("records", []).insert(0, record)
            save_db(db)
        self.send_json(record, HTTPStatus.CREATED)

    def handle_upload(self, task_id):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            self.send_json({"error": "图片或文件大小需在 50MB 以内"}, HTTPStatus.BAD_REQUEST)
            return
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self.send_json({"error": "请使用文件上传格式"}, HTTPStatus.BAD_REQUEST)
            return
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type})
        field = form["file"] if "file" in form else None
        if field is None or not getattr(field, "filename", ""):
            self.send_json({"error": "没有选择文件"}, HTTPStatus.BAD_REQUEST)
            return
        original_name = os.path.basename(field.filename)
        mime = field.type or mimetypes.guess_type(original_name)[0] or "application/octet-stream"
        if mime.startswith("video/"):
            self.send_json({"error": "本地版暂不保存视频文件"}, HTTPStatus.BAD_REQUEST)
            return
        suffix = Path(original_name).suffix[:12]
        stored_name = f"{uuid.uuid4().hex}{suffix}"
        target = ATTACHMENTS_DIR / stored_name
        data = field.file.read()
        target.write_bytes(data)
        attachment = {"id": str(uuid.uuid4()), "name": original_name, "stored_name": stored_name, "mime": mime, "size": len(data), "created_at": now_iso()}
        with LOCK:
            db = load_db()
            task = find_task(db, task_id)
            if not task:
                target.unlink(missing_ok=True)
                self.send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                return
            task.setdefault("attachments", []).insert(0, attachment)
            save_db(db)
        self.send_json(attachment, HTTPStatus.CREATED)

    def do_PATCH(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            payload = self.read_json()
            with LOCK:
                db = load_db()
                if path.startswith("/api/tasks/"):
                    task = find_task(db, path.split("/")[3])
                    if not task:
                        self.send_json({"error": "任务不存在"}, HTTPStatus.NOT_FOUND)
                        return
                    allowed = {"title", "status", "priority", "due_date", "due_time", "description", "reminder_time"}
                    for key in allowed:
                        if key in payload:
                            task[key] = payload[key]
                    if payload.get("status") == "已完成" and not task.get("completed_at"):
                        task["completed_at"] = now_iso()
                    if payload.get("status") != "已完成":
                        task["completed_at"] = ""
                    save_db(db)
                    self.send_json(task)
                    return
                if path.startswith("/api/knowledge/"):
                    entry = next((item for item in db["knowledge"] if item["id"] == path.split("/")[3]), None)
                    if not entry:
                        self.send_json({"error": "经验不存在"}, HTTPStatus.NOT_FOUND)
                        return
                    for key in ("title", "content", "tags"):
                        if key in payload:
                            entry[key] = payload[key]
                    entry["updated_at"] = now_iso()
                    save_db(db)
                    self.send_json(entry)
                    return
                if path == "/api/settings":
                    db["settings"].update({key: payload[key] for key in ("reminder_time", "local_notifications") if key in payload})
                    save_db(db)
                    self.send_json(db["settings"])
                    return
            self.send_json({"error": "未找到"}, HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc) or "请求格式错误"}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_json({"error": f"服务器错误：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def main():
    DATA_DIR.mkdir(exist_ok=True)
    ATTACHMENTS_DIR.mkdir(exist_ok=True)
    if not DB_FILE.exists():
        save_db(default_db())
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("个人工作台已启动")
    print(f"电脑访问：http://127.0.0.1:{PORT}")
    print(f"手机访问：http://{lan_ip()}:{PORT}（需与电脑连接同一 Wi‑Fi）")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
