#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上海科技大学 GenAI 凭据自动刷新后台服务。

策略:
1. 每 30 分钟检查一次本地 JWT 过期时间（纯本地计算，零网络开销）。
2. 距过期不足 24 小时或已过期时，自动启动专用浏览器完成续期:
   - SSO 会话 cookie 存活: 全程零交互，秒级完成（自动点击统一认证/确认）。
   - SSO 会话失效: 若存在 credentials.txt（username= / password=）则自动填表登录；
     否则保持窗口打开并弹窗提示用户手动登录一次。
3. 刷新成功后写回 token.txt（代理按请求热读取，无需重启）。

所有浏览器交互通过 Chrome DevTools Protocol (CDP) 完成，复用 get_token.py 的
专用浏览器配置（%LOCALAPPDATA%\\ShanghaiTechGenAIProxy\\browser-profile），
SSO 会话 cookie 因此得以保留，实现长期免登录。
"""

import json
import os
import threading
import time
import traceback
from pathlib import Path

import httpx

try:
    import websocket  # websocket-client
except ImportError:
    websocket = None

import get_token as gt

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = BASE_DIR / "credentials.txt"
CHECK_INTERVAL_SECONDS = 30 * 60          # 常规巡检间隔
EXPIRY_MARGIN_SECONDS = 2 * 3600          # 距过期不足 2 小时即续期（token 有效期约 24h）
LOGIN_WAIT_SECONDS = 300                  # 单次续期最长等待（等 SSO 跳转/用户输入）
REPROMPT_SECONDS = 3600                   # 需要人工登录时的重复弹窗间隔

_state_lock = threading.Lock()
_active_streams = 0                        # 正在进行的流式响应数（续期需等待其结束）
_refresh_lock = threading.Lock()
_last_user_prompt = 0.0
_last_error = None


def _log(msg: str) -> None:
    print(f"[token-auto] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def _read_credentials():
    """读取 credentials.txt（username= / password= 行）。文件不存在返回 (None, None)。"""
    if not CREDENTIALS_FILE.exists():
        return None, None
    user = pwd = None
    try:
        for line in CREDENTIALS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("username="):
                user = line.split("=", 1)[1].strip()
            elif line.startswith("password="):
                pwd = line.split("=", 1)[1].strip()
    except Exception:
        pass
    return (user or None), (pwd or None)


def _cdp_eval(ws_url: str, expression: str, timeout: float = 3.0):
    """在指定页面执行 JS 并返回结果值。"""
    if websocket is None:
        raise RuntimeError("缺少 websocket-client")
    conn = websocket.create_connection(
        ws_url, timeout=timeout, origin="http://127.0.0.1",
        http_proxy_host=None, http_proxy_port=None,
    )
    try:
        conn.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True},
        }))
        while True:
            response = json.loads(conn.recv())
            if response.get("id") == 1:
                return response.get("result", {}).get("result", {}).get("value")
    finally:
        conn.close()


def _click_by_text(ws_url: str, texts) -> bool:
    """点击页面上文本匹配的按钮/链接（去除空格后包含匹配）。"""
    wanted = [t.replace(" ", "") for t in texts]
    expr = """(() => {
      const els = [...document.querySelectorAll('button, a, span[role=button]')];
      for (const want of %s) {
        const b = els.find(e => (e.innerText||'').replace(/\\\\s/g,'').includes(want));
        if (b) { b.click(); return 'clicked:' + want; }
      }
      return 'notfound';
    })()""" % json.dumps(wanted, ensure_ascii=False)
    try:
        return str(_cdp_eval(ws_url, expr) or "").startswith("clicked")
    except Exception:
        return False


def _fill_sso_form(ws_url: str, user: str, pwd: str) -> bool:
    """自动填写统一身份认证表单（兼容 Vue/CAS 的 input 事件触发）。"""
    expr = """(() => {
      const u = document.querySelector('input[name="username"], #username, input[placeholder*="学号"]');
      const p = document.querySelector('input[type="password"], #password, input[placeholder*="密码"]');
      if (!u || !p) return 'fields-not-found';
      const set = (el, v) => {
        const desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
        if (desc && desc.set) { desc.set.call(el, v); } else { el.value = v; }
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
      };
      if (u.value) return 'already-filled';
      set(u, %s); set(p, %s);
      return 'filled';
    })()""" % (json.dumps(user), json.dumps(pwd))
    try:
        return _cdp_eval(ws_url, expr) == "filled"
    except Exception:
        return False


def _notify_user(title: str, message: str) -> None:
    """非阻塞弹窗提醒（独立线程，避免卡住刷新循环）。"""
    def _show():
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x40)
        except Exception:
            pass
    threading.Thread(target=_show, daemon=True).start()


def refresh_once(max_wait: int = LOGIN_WAIT_SECONDS):
    """执行一次完整续期。成功返回新 token，失败/超时返回 None。"""
    port, browser_name = gt.launch_debug_browser()
    endpoint = f"http://127.0.0.1:{port}/json/list"
    _log(f"{browser_name.title()} 调试浏览器已就绪 (port {port})，开始自动续期……")

    deadline = time.time() + max_wait
    sso_clicked = False
    form_filled = False
    submitted = False

    while time.time() < deadline:
        try:
            pages = httpx.get(endpoint, timeout=2).json()
        except Exception:
            time.sleep(1)
            continue

        for page in pages:
            url = page.get("url", "")
            if page.get("type") != "page" or "shanghaitech.edu.cn" not in url:
                continue
            ws = page.get("webSocketDebuggerUrl")
            if not ws:
                continue

            # 1) 任意学校页面：尝试读取 Token（登录成功即出现）
            try:
                token = gt.read_token_from_page(ws)
                if token:
                    return token
            except Exception:
                pass

            # 2) GenAI 登录页：点「统一身份认证登录」，处理各种弹窗
            if "genai.shanghaitech.edu.cn" in url and "/user/login" in url:
                if _click_by_text(ws, ["重新登录"]):
                    time.sleep(1.5)
                _click_by_text(ws, ["统一身份认证登录"])
                _click_by_text(ws, ["确 认", "确认"])
                sso_clicked = True

            # 3) SSO 表单页：填账号密码（如配置了凭据）
            if "ids.shanghaitech.edu.cn" in url:
                user, pwd = _read_credentials()
                if user and pwd and not form_filled:
                    if _fill_sso_form(ws, user, pwd):
                        form_filled = True
                        _log("SSO 表单已自动填写")
                if form_filled and not submitted:
                    try:
                        _cdp_eval(ws, "document.querySelector('#login_submit')?.click()")
                        submitted = True
                        _log("SSO 表单已提交，等待跳转……")
                    except Exception:
                        pass

        time.sleep(1)

    return None


def needs_refresh() -> bool:
    """本地判断凭据是否需要续期（已过期或 24 小时内到期）。"""
    try:
        token = gt.TOKEN_FILE.read_text(encoding="utf-8").strip() if gt.TOKEN_FILE.exists() else ""
    except Exception:
        return True
    if not token:
        return True
    expiry = gt.decode_expiry(token)
    if expiry is None:
        return False  # 无法解析时交由上游状态判断，避免误刷
    return expiry <= int(time.time()) + EXPIRY_MARGIN_SECONDS


def _attempt_refresh_cycle():
    """单次续期尝试：成功返回 True；需要人工介入返回 False。"""
    global _last_user_prompt
    _wait_streams_drain()
    try:
        token = refresh_once()
    except Exception as exc:
        _log(f"续期异常: {exc}")
        token = None

    if token:
        if len(token.split(".")) != 3 or not gt.verify_token(token):
            _log("获取到的 Token 未通过上游验证，放弃保存。")
            return False
        gt.save_token(token)
        expiry = gt.decode_expiry(token)
        expiry_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expiry)) if expiry else "未知"
        _log(f"✓ Token 自动续期成功，新凭据有效期至 {expiry_text}")
        return True

    # 失败：区分「凭据仍有效只是提前续期失败」与「已过期需要人工」
    expired = needs_refresh_force_expired()
    if expired:
        now = time.time()
        if now - _last_user_prompt > REPROMPT_SECONDS:
            _last_user_prompt = now
            _notify_user(
                "GenAI 凭据需要手动登录",
                "自动续期未能完成（SSO 会话已失效且未配置自动登录）。\n\n"
                "已打开的浏览器窗口中，请完成学校统一身份认证登录，\n"
                "登录成功后凭据会自动保存，无需其他操作。",
            )
            _log("需要人工登录：请在打开的浏览器窗口中完成登录（窗口保持打开）。")
    else:
        _log("静默续期未完成（当前凭据仍有效），将在下个巡检周期重试。")
    return False


def needs_refresh_force_expired() -> bool:
    """严格判断凭据是否已过期（用于决定是否打扰用户）。"""
    try:
        token = gt.TOKEN_FILE.read_text(encoding="utf-8").strip() if gt.TOKEN_FILE.exists() else ""
        expiry = gt.decode_expiry(token) if token else None
        return expiry is None or expiry <= int(time.time())
    except Exception:
        return True


def stream_started() -> None:
    global _active_streams
    with _state_lock:
        _active_streams += 1


def stream_ended() -> None:
    global _active_streams
    with _state_lock:
        _active_streams = max(0, _active_streams - 1)


def _wait_streams_drain(max_wait: float = 90.0) -> None:
    """等待所有流式响应结束（重新登录可能使当前会话失效，绝不打断生成中的流）。"""
    deadline = time.time() + max_wait
    while _active_streams > 0 and time.time() < deadline:
        _log(f"检测到 { _active_streams } 个流式响应进行中，推迟续期……")
        time.sleep(5)


def kick() -> None:
    """外部触发立即续期（如上游返回凭据失效时）。"""
    with _state_lock:
        _kick_event.set()


_kick_event = threading.Event()


def _loop():
    global _last_user_prompt
    _log("自动刷新服务已启动（巡检间隔 30 分钟，24 小时内到期自动续期）。")
    if not gt.find_browsers():
        _log("未找到 Chrome/Edge，自动刷新不可用。")
        return
    while True:
        kicked = _kick_event.wait(timeout=CHECK_INTERVAL_SECONDS)
        _kick_event.clear()
        with _refresh_lock:
            try:
                if kicked or needs_refresh():
                    _log("开始凭据续期……")
                    _attempt_refresh_cycle()
            except Exception:
                _log("续期循环异常: " + traceback.format_exc(limit=3))


def start_background() -> None:
    threading.Thread(target=_loop, daemon=True, name="token-auto-refresh").start()
