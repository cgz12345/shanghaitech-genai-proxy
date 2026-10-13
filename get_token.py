import argparse
import base64
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

try:
    import websocket
except ImportError:
    websocket = None


BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BASE_DIR / "token.txt"
LOGIN_URL = "https://genai.shanghaitech.edu.cn"
MODEL_LIST_URL = "https://genai.shanghaitech.edu.cn/htk/ai/aiModel/list"


def decode_expiry(token: str):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def save_token(token: str) -> None:
    temporary = TOKEN_FILE.with_suffix(".tmp")
    temporary.write_text(token.strip(), encoding="utf-8")
    os.replace(temporary, TOKEN_FILE)


def verify_token(token: str) -> bool:
    try:
        response = httpx.get(
            MODEL_LIST_URL,
            params={"pageNo": 1, "pageSize": 1, "showStatusList": "2,3"},
            headers={"X-Access-Token": token},
            verify=False,
            timeout=15,
        )
        if response.status_code != 200:
            return False
        data = response.json()
        return bool(data.get("success") and data.get("result"))
    except (httpx.HTTPError, ValueError):
        return False


def notify_success(expiry_text: str) -> None:
    message = (
        "上海科技大学 GenAI Token 获取成功！\n\n"
        f"本地到期时间：{expiry_text}\n"
        "上游验证已通过，代理已即时生效，无需重启。"
    )
    try:
        ctypes.windll.user32.MessageBoxW(0, message, "GenAI Token 获取成功", 0x40)
    except (AttributeError, OSError):
        pass


def find_browsers():
    candidates = [
        Path(os.environ.get("ProgramFiles", ""))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", ""))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles", ""))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles(x86)", ""))
        / "Microsoft/Edge/Application/msedge.exe",
    ]
    return list(dict.fromkeys(path for path in candidates if path.is_file()))


def find_free_port():
    for port in range(9229, 9259):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("找不到可用的浏览器调试端口")


def find_existing_debug_browser():
    for port in range(9229, 9259):
        try:
            version = httpx.get(
                f"http://127.0.0.1:{port}/json/version", timeout=0.25
            ).json()
            pages = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=0.25).json()
            if any(
                "genai.shanghaitech.edu.cn" in page.get("url", "") for page in pages
            ):
                product = str(version.get("Browser", "browser")).lower()
                name = "edge" if "edge" in product else "chrome"
                return port, name
        except (httpx.HTTPError, ValueError):
            pass
    return None


def read_token_from_page(debugger_url: str) -> str:
    if websocket is None:
        raise RuntimeError("缺少 websocket-client，请先运行 install_deps.bat")
    connection = websocket.create_connection(
        debugger_url,
        timeout=3,
        origin="http://127.0.0.1",
        http_proxy_host=None,
        http_proxy_port=None,
    )
    try:
        expression = """(() => {
          const raw = localStorage.getItem('pro__Access-Token');
          if (!raw) return '';
          try { const data = JSON.parse(raw); return data && data.value || ''; }
          catch (_) { return raw; }
        })()"""
        connection.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {"expression": expression, "returnByValue": True},
                }
            )
        )
        while True:
            response = json.loads(connection.recv())
            if response.get("id") == 1:
                return (
                    response.get("result", {})
                    .get("result", {})
                    .get("value", "")
                    .strip()
                )
    finally:
        connection.close()


def launch_debug_browser():
    existing = find_existing_debug_browser()
    if existing:
        return existing
    browsers = find_browsers()
    if not browsers:
        raise RuntimeError("未找到 Chrome 或 Edge")
    profile_root = Path(os.environ.get("LOCALAPPDATA", str(BASE_DIR)))
    errors = []
    for browser in browsers:
        port = find_free_port()
        browser_name = "edge" if "edge" in browser.name.lower() else "chrome"
        profile = profile_root / f"ShanghaiTechGenAIProxy/{browser_name}-profile"
        profile.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            [
                str(browser),
                f"--remote-debugging-port={port}",
                "--remote-allow-origins=*",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--new-window",
                LOGIN_URL,
            ]
        )
        endpoint = f"http://127.0.0.1:{port}/json/version"
        for _ in range(40):
            if process.poll() is not None:
                break
            try:
                response = httpx.get(endpoint, timeout=1)
                if response.status_code == 200:
                    return port, browser_name
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        errors.append(f"{browser_name} 未开放调试端口")
    raise RuntimeError("；".join(errors))


def automatic_login(timeout_seconds: int = 600) -> str:
    port, browser_name = launch_debug_browser()
    endpoint = f"http://127.0.0.1:{port}/json/list"
    deadline = time.time() + timeout_seconds
    print(
        f"{browser_name.title()} 已打开且调试连接正常。请完成学校登录；登录成功后 Token 会自动保存。"
    )
    print("等待登录中，可按 Ctrl+C 取消……")
    while time.time() < deadline:
        try:
            pages = httpx.get(endpoint, timeout=2).json()
            for page in pages:
                if (
                    page.get("type") == "page"
                    and "genai.shanghaitech.edu.cn" in page.get("url", "")
                    and page.get("webSocketDebuggerUrl")
                ):
                    token = read_token_from_page(page["webSocketDebuggerUrl"])
                    if token:
                        return token
        except Exception:
            pass
        time.sleep(1)
    raise TimeoutError("等待登录超时，请重新运行 get_token.bat")


def manual_input() -> str:
    print("请粘贴浏览器 localStorage 中 pro__Access-Token 的 value：")
    token = input("> ").strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        token = token[1:-1]
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description="自动获取上海科技大学 GenAI Token")
    parser.add_argument("--manual", action="store_true", help="使用手工粘贴模式")
    parser.add_argument("--timeout", type=int, default=600, help="等待登录的秒数")
    args = parser.parse_args()
    print("=" * 64)
    print("上海科技大学 GenAI 登录凭据配置助手")
    print("=" * 64)
    try:
        token = manual_input() if args.manual else automatic_login(args.timeout)
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130
    except Exception as exc:
        print(f"\n自动获取失败：{exc}")
        print("可运行 get_token.bat --manual 使用手工模式。")
        return 1
    if len(token.split(".")) != 3:
        print("获取到的内容不是有效 JWT，未覆盖现有 token.txt。")
        return 1
    expiry = decode_expiry(token)
    if expiry is not None and expiry <= int(time.time()):
        print("获取到的 Token 已经过期，未覆盖现有 token.txt。")
        return 1
    if not verify_token(token):
        print("上游验证失败，未覆盖现有 token.txt。请确认网页已经登录成功。")
        return 1
    save_token(token)
    expiry_text = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expiry)) if expiry else "未知"
    )
    print(f"\nToken 已自动保存到 {TOKEN_FILE}")
    print(f"本地到期时间：{expiry_text}")
    print("上游验证通过，Token 已即时生效，无需重启代理。")
    notify_success(expiry_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
