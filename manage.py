"""Local administration. Starting an enabled local model includes a readiness inference."""
import argparse
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import subprocess
import time
import webbrowser
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

from app.config import load_settings
from app.errors import XiaoNuoError
from app.main import build_router


def initialize():
    target = ROOT / ".env.local"
    if target.is_symlink() or (target.exists() and (not target.is_file() or target.stat().st_nlink > 1)):
        raise ValueError("Refusing to write a non-regular env file")
    settings = load_settings(target)
    if settings.admin_token:
        if len(settings.admin_token) < 32:
            raise ValueError("Existing admin token is too short; update it locally")
        print("Local authentication is already configured. No secrets were displayed.")
        return
    original = target.read_text(encoding="utf-8") if target.exists() else ""
    # Preserve an existing file's Windows ACL instead of replacing its directory entry.
    with target.open("a", encoding="utf-8", newline="\n") as output:
        if original and not original.endswith("\n"):
            output.write("\n")
        output.write(f"XIAONUO_ADMIN_TOKEN={secrets.token_urlsafe(48)}\n")
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    print("Local authentication saved. No secrets were displayed; no remote requests were made.")


def local_request(path: str, body: dict | None = None, *, timeout_seconds: int = 100):
    settings = load_settings()
    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request("http://127.0.0.1:8765" + path, data=payload,
                      headers={"Authorization": "Bearer " + settings.admin_token, "Content-Type": "application/json"})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=timeout_seconds) as response:
            return json.load(response)
    except HTTPError as error:
        raw = error.read(8192)
        error.close()
        try:
            code = json.loads(raw).get("error", "LOCAL_SERVER_ERROR")
        except (ValueError, AttributeError):
            code = "LOCAL_SERVER_ERROR"
        raise XiaoNuoError(code) from None
    except (URLError, TimeoutError, OSError):
        raise XiaoNuoError("LOCAL_BACKEND_NOT_RUNNING") from None


@contextmanager
def process_lock(name, timeout=90):
    """An OS lock releases on crashes; stale PID files never authorize a kill."""
    directory = ROOT / 'data'
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / name
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink > 1)):
        raise ValueError('Refusing a non-regular lock file')
    with path.open('a+b') as handle:
        if path.stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise XiaoNuoError('LOCAL_BACKEND_BUSY') from None
                time.sleep(0.2)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ensure_backend():
    with process_lock('backend-start.lock'):
        _ensure_backend()


def _ensure_backend():
    initialize()
    try:
        local_request("/api/v1/status", timeout_seconds=2)
    except XiaoNuoError as error:
        if error.code != "LOCAL_BACKEND_NOT_RUNNING":
            raise
        if (ROOT/'data/worker-service/control.json').exists():
            from app.worker_service import start
            start(ROOT)
            until=time.monotonic()+180
            while time.monotonic()<until:
                try:
                    local_request('/api/v1/status',timeout_seconds=2)
                    return
                except XiaoNuoError as startup_error:
                    if startup_error.code!='LOCAL_BACKEND_NOT_RUNNING':raise
                    time.sleep(.5)
            raise XiaoNuoError('LOCAL_BACKEND_START_FAILED')
        logs = ROOT / "data"
        logs.mkdir(mode=0o700, exist_ok=True)
        with (logs / "console-server.log").open("ab") as output:
            process=subprocess.Popen([sys.executable, str(ROOT / "manage.py"), "serve"], cwd=ROOT,
                             stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                             start_new_session=os.name != "nt")
        (logs/'local-server.json').write_text(json.dumps({'pid':process.pid,'url':'http://127.0.0.1:8765','project':str(ROOT)}),encoding='utf-8')
        # Hashing and loading the local model can take longer than five seconds.
        deadline=time.monotonic()+60
        while time.monotonic()<deadline and process.poll() is None:
            try:
                local_request("/api/v1/status", timeout_seconds=2)
                break
            except XiaoNuoError as startup_error:
                if startup_error.code != "LOCAL_BACKEND_NOT_RUNNING":
                    raise
                time.sleep(0.5)
        else:
            raise XiaoNuoError("LOCAL_BACKEND_START_FAILED")


def open_console():
    ensure_backend()
    result = local_request("/api/v1/session/bootstrap", {}, timeout_seconds=2)
    if not webbrowser.open("http://127.0.0.1:8765/#ticket=" + result["ticket"]):
        raise XiaoNuoError("DEFAULT_BROWSER_NOT_AVAILABLE")
    print("小糯控制台已打开。模型 API 密钥没有发送到浏览器。")


def main():
    parser = argparse.ArgumentParser(description="小糯本地后端管理")
    parser.add_argument("command", choices=["init", "serve", "status", "chat", "verify-free", "open"])
    parser.add_argument("--provider", choices=["groq", "gemini"])
    args = parser.parse_args()
    try:
        if args.command == "init":
            initialize()
        elif args.command == "open":
            open_console()
        elif args.command == "serve":
            import uvicorn
            sys.path.insert(0, str(ROOT / "backend"))
            with process_lock('backend-runtime.lock', timeout=0):
                uvicorn.run("app.main:create_app", factory=True, host="127.0.0.1", port=8765,
                            workers=1, access_log=False, log_level="warning", timeout_keep_alive=5,
                            proxy_headers=False)
        elif args.command == "status":
            print(json.dumps(local_request("/api/v1/status"), ensure_ascii=False, indent=2))
        elif args.command == "verify-free":
            if not args.provider:
                parser.error("verify-free requires --provider")
            router = build_router(load_settings())
            print(json.dumps(router.verify_free(args.provider), ensure_ascii=False, indent=2))
        elif args.command == "chat":
            conversation = None
            print("输入 /quit 结束。已启用的本机模型可离线回答；远程调用仍受单独开关控制。")
            while True:
                try:
                    text = input("你：").strip()
                except EOFError:
                    return
                if text == "/quit":
                    return
                if not text:
                    continue
                body = {"text": text, "request_id": str(uuid4()), "conversation_id": conversation}
                try:
                    answer = local_request("/api/v1/chat", body)
                    conversation = answer["conversation_id"]
                    print("小糯：" + answer["reply_text"])
                except XiaoNuoError as error:
                    print("状态：" + error.code)
    except (XiaoNuoError, ValueError) as error:
        print("Configuration/request error: " + str(error), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
