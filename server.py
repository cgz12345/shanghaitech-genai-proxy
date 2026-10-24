#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ShanghaiTech GenAI to OpenAI-Compatible API Proxy Server
上科大 GenAI 网页服务转标准 OpenAI 接口代理（含多模态：图片理解 + PDF 文档）

Token 配置（三选一，优先级从高到低）:
  1. 环境变量  SHANGHAITECH_TOKEN
  2. 本目录下  token.txt
  3. 本目录下  token_1.txt
获取方法见 README.md 或运行 get_token.py
"""

import os
import sys
import io
import json
import re
import time
import uuid
import base64
import socket
import hmac
import ipaddress
from urllib.parse import urlparse
from typing import Dict, Any, List, Tuple, Generator, Optional
from flask import Flask, request, Response, jsonify
import httpx

try:
    from PIL import Image as PILImage

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    from waitress import serve as waitress_serve

    WAITRESS_AVAILABLE = True
except ImportError:
    WAITRESS_AVAILABLE = False

try:
    from tool_calling_engine import (
        build_tools_system_prompt,
        extract_tool_calls,
        clean_leak_tokens,
    )

    TOOL_ENGINE_AVAILABLE = True
except ImportError:
    TOOL_ENGINE_AVAILABLE = False

try:
    import token_auto_refresh

    TOKEN_AUTO_REFRESH_AVAILABLE = True
except ImportError:
    TOKEN_AUTO_REFRESH_AVAILABLE = False

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(
    os.environ.get("PROXY_MAX_REQUEST_BYTES", 32 * 1024 * 1024)
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SHANGHAITECH_BASE_URL = "https://genai.shanghaitech.edu.cn/htk"
PIC_BASE = "https://genaipic.shanghaitech.edu.cn"
# 前端图片上传使用的固定 token（来自网页端 JS）
PIC_UPLOAD_TOKEN = "2ea38f293adb4abca21132feba61eaa3"

# 可配置自定义访问 API Key（设为空字符串可关闭鉴权）
LOCAL_API_KEY = os.environ.get("PROXY_API_KEY", "sk-shanghaitech-genai")
MAX_REMOTE_IMAGE_BYTES = int(os.environ.get("PROXY_MAX_IMAGE_BYTES", 20 * 1024 * 1024))


def validate_remote_url(url: str) -> None:
    """阻止服务器端请求被用来访问本机、内网和链路本地地址。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("image_url must be an http(s) URL or data URL")
    if os.environ.get("ALLOW_PRIVATE_IMAGE_URLS") == "1":
        return
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("private, loopback, and link-local image URLs are blocked")
    try:
        literal_ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not literal_ip.is_global:
        raise ValueError("private, loopback, and link-local image URLs are blocked")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, port)}
    proxy_mapping = ipaddress.ip_network("198.18.0.0/15")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        # Codex 桌面网络层会把公网域名映射到 RFC 2544 测试段；域名可放行，
        # 但用户直接提供该段 IP 时已在上面的 literal_ip 检查中拒绝。
        if not ip.is_global and ip not in proxy_mapping:
            raise ValueError("private, loopback, and link-local image URLs are blocked")


def parse_requested_port() -> int:
    """从命令行参数或环境变量解析请求的初始端口，默认为 8000"""
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg in ("--port", "-p") and i < len(sys.argv):
            try:
                return int(sys.argv[i])
            except ValueError:
                pass
        elif arg.isdigit():
            try:
                return int(arg)
            except ValueError:
                pass
    return int(os.environ.get("PORT", "8000"))


def is_port_free(port: int, host: str = "0.0.0.0") -> bool:
    """真实测试该端口能否被当前程序独占绑定（完美兼容 Windows 与 Linux）"""
    # 1. 先尝试连接探测是否已有服务在监听
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as test_sock:
            test_sock.settimeout(0.2)
            if test_sock.connect_ex(("127.0.0.1", port)) == 0:
                return False
    except Exception:
        pass

    # 2. 尝试真正独占绑定（Windows 使用 SO_EXCLUSIVEADDRUSE 防止虚假重用）
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind((host, port))
            return True
    except Exception:
        return False


def is_existing_proxy(port: int) -> bool:
    """判断指定端口是否已经运行本项目的健康实例。"""
    try:
        health_resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
        root_resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=1.0)
        health = health_resp.json()
        root = root_resp.json()
        return bool(
            health_resp.status_code == 200
            and health.get("status") in ("ok", "degraded")
            and root_resp.status_code == 200
            and root.get("service") == "ShanghaiTech GenAI OpenAI Proxy"
        )
    except (httpx.HTTPError, ValueError):
        return False


def resolve_actual_port(requested_port: int, max_offset: int = 100) -> int:
    """
    智能端口解析与自动避让：
    如果指定端口未被占用，直接使用；
    如果已被占用，自动向下查找下一个可用端口，确保服务绝对能成功启动！
    """
    if is_port_free(requested_port):
        return requested_port

    print(f"\n   [!] 提示: 端口 {requested_port} 已被占用，正在自动查找可用端口...")
    for candidate in range(requested_port + 1, requested_port + max_offset):
        if is_port_free(candidate):
            print(f"   [√] 成功找到空闲端口: {candidate} (已自动避让冲突)\n")
            return candidate

    raise OSError(
        f"无法在端口区间 [{requested_port}, {requested_port + max_offset}) 找到可用端口"
    )


# ---------------- Token 管理 ----------------


def get_shanghaitech_token() -> str:
    """获取上海科技大学 GenAI 的 X-Access-Token（环境变量 > token.txt > token_1.txt）"""
    env = os.environ.get("SHANGHAITECH_TOKEN", "").strip()
    if env:
        return env
    for name in ("token.txt", "token_1.txt"):
        p = os.path.join(BASE_DIR, name)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                val = f.read().strip()
            if val:
                return val
    return ""


def get_token_expiry(token: str = "") -> Optional[int]:
    """读取 JWT exp；无法解析或没有 exp 时返回 None，不验证签名。"""
    token = token or get_shanghaitech_token()
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return int(decoded["exp"]) if decoded.get("exp") else None
    except (IndexError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def upstream_auth_status() -> str:
    token = get_shanghaitech_token()
    if not token:
        return "missing"
    expiry = get_token_expiry(token)
    if expiry is not None and expiry <= int(time.time()):
        return "expired"
    return "valid"


def token_source() -> str:
    if os.environ.get("SHANGHAITECH_TOKEN", "").strip():
        return "环境变量 SHANGHAITECH_TOKEN"
    for name in ("token.txt", "token_1.txt"):
        if os.path.exists(os.path.join(BASE_DIR, name)):
            return name
    return "未配置"


def get_headers() -> Dict[str, str]:
    return {
        "X-Access-Token": get_shanghaitech_token(),
        "Content-Type": "application/json",
        "Accept": "*/*",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    }


# ---------------- 模型列表 ----------------

_models_cache = {"data": [], "timestamp": 0}


def fetch_shanghaitech_models() -> list:
    now = time.time()
    if _models_cache["data"] and (now - _models_cache["timestamp"] < 300):
        return _models_cache["data"]
    try:
        r = httpx.get(
            f"{SHANGHAITECH_BASE_URL}/ai/aiModel/list",
            params={"pageNo": 1, "pageSize": 999, "showStatusList": "2,3"},
            headers=get_headers(),
            verify=False,
            timeout=15.0,
        )
        if r.status_code == 200:
            res = r.json()
            if res.get("success"):
                records = res.get("result", {}).get("records", [])
                _models_cache["data"] = records
                _models_cache["timestamp"] = now
                return records
    except Exception as e:
        print(f"[Error] Failed to fetch models from ShanghaiTech: {e}", file=sys.stderr)
    return _models_cache["data"]


def _log_ts(msg: str) -> None:
    """带时间戳的控制台日志（断流/续期等关键事件必须可追溯）。"""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def find_model_config(model_id: str) -> Dict[str, Any]:
    """按模型配置匹配：同时接受学校内部 ID（aiType）与网页显示名（aiName），大小写不敏感。"""
    models = fetch_shanghaitech_models()
    wanted = (model_id or "").strip().lower()
    for m in models:
        if str(m.get("aiType") or "").strip().lower() == wanted:
            return m
    for m in models:
        if str(m.get("aiName") or "").strip().lower() == wanted:
            return m
    if models:
        return {}
    return {
        "aiType": model_id,
        "rootAiType": "azure" if "gpt" in model_id.lower() else "xinference",
        "enableDeepThink": 0,
    }


# ---------------- 网络层 ----------------


@app.before_request
def check_auth():
    if request.path in ["/", "/health"] or request.method == "OPTIONS":
        return None
    if LOCAL_API_KEY:
        auth_header = request.headers.get("Authorization", "")
        supplied = auth_header[7:] if auth_header.startswith("Bearer ") else ""
        valid_keys = [LOCAL_API_KEY]
        upstream_token = get_shanghaitech_token()
        if upstream_token:
            valid_keys.append(upstream_token)
        if not supplied or not any(
            hmac.compare_digest(supplied, key) for key in valid_keys
        ):
            return jsonify(
                {
                    "error": {
                        "message": "Incorrect API key provided.",
                        "type": "invalid_request_error",
                        "param": None,
                        "code": "invalid_api_key",
                    }
                }
            ), 401
    if request.path.startswith("/v1/") and upstream_auth_status() != "valid":
        # 触发后台自动续期（若自动刷新模块可用），并向前端返回明确错误
        if TOKEN_AUTO_REFRESH_AVAILABLE:
            token_auto_refresh.kick()
        return jsonify(
            {
                "error": {
                    "message": "ShanghaiTech upstream token is missing or expired. Run get_token.bat and restart the proxy.",
                    "type": "server_error",
                    "param": None,
                    "code": "upstream_auth_expired",
                }
            }
        ), 503
    return None


@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/", methods=["GET"])
def index():
    return jsonify(
        {
            "service": "ShanghaiTech GenAI OpenAI Proxy",
            "status": "running",
            "models_endpoint": "/v1/models",
            "chat_endpoint": "/v1/chat/completions",
            "images_endpoint": "/v1/images/generations",
            "speech_endpoint": "/v1/audio/speech",
            "transcriptions_endpoint": "/v1/audio/transcriptions",
            "multimodal": "image_url + file supported",
        }
    )


@app.route("/health", methods=["GET"])
def health():
    auth_status = upstream_auth_status()
    return jsonify(
        {
            "status": "ok" if auth_status == "valid" else "degraded",
            "upstream_auth": auth_status,
            "token_expires_at": get_token_expiry(),
            "time": int(time.time()),
        }
    )


@app.route("/v1/models", methods=["GET"])
def list_models():
    """返回标准 OpenAI 模型列表"""
    raw_models = fetch_shanghaitech_models()
    openai_models = []
    if raw_models:
        for m in raw_models:
            display_name = (m.get("aiName") or "").strip() or m.get("aiType")
            openai_models.append(
                {
                    "id": display_name,
                    "object": "model",
                    "created": 1700000000,
                    "owned_by": m.get("rootAiType", "shanghaitech"),
                    "permission": [],
                    "root": m.get("aiType"),
                    "parent": None,
                    "capabilities": {
                        "chat": str(m.get("status")) in ("1", "3"),
                        "vision": str(m.get("status")) == "3",
                        "documents": str(m.get("status")) == "3",
                        "image_generation": str(m.get("status")) == "2",
                        "deep_thinking": str(m.get("enableDeepThink")) == "1",
                    },
                }
            )
    else:
        for mid in [
            "gpt-6-astra",
            "GPT-5.6-SOL",
            "deepseek-chat",
            "deepseek-pro",
            "qwen-instruct",
        ]:
            openai_models.append(
                {
                    "id": mid,
                    "object": "model",
                    "created": 1700000000,
                    "owned_by": "shanghaitech",
                }
            )
    return jsonify({"object": "list", "data": openai_models})


# ---------------- 多模态支持 ----------------

MIME_EXT_MAP = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def extract_multimodal_parts(
    messages: List[Dict[str, Any]],
) -> Tuple[str, List[Dict[str, str]], List[str], List[Tuple[str, str, str]]]:
    """
    解析 OpenAI messages:
    - 返回 (chat_info, 纯文本化后的 messages, image_url 列表, (filename, bytes, mime) 文档列表)
    - 图片: {"type":"image_url","image_url":{"url": "data:...|https://..."}}，兼容 {"type":"input_image","image_url":"..."}
    - 文档: {"type":"file","file":{"filename":..., "file_data":"data:application/pdf;base64,..."}}
    """
    chat_info = ""
    plain_messages: List[Dict[str, str]] = []
    image_refs: List[str] = []
    file_items: List[Tuple[str, bytes, str]] = []
    tool_names_by_id: Dict[str, str] = {}

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")
        tool_calls = msg.get("tool_calls") or []

        # 兼容 Agent 第 2 轮中的 tool / function 角色消息
        if role in ("tool", "function"):
            tool_name = (
                msg.get("name")
                or tool_names_by_id.get(msg.get("tool_call_id", ""))
                or "tool"
            )
            text_val = str(content) if content is not None else ""
            formatted = f"[工具 {tool_name} 执行结果]: {text_val}"
            plain_messages.append({"role": "user", "content": formatted})
            chat_info = formatted
            continue

        # 兼容 assistant 发出的包含 tool_calls 的历史消息（此时 content 往往是 None）
        if role == "assistant" and tool_calls:
            tool_descs = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                if tc.get("id") and fn.get("name"):
                    tool_names_by_id[tc["id"]] = fn["name"]
                tool_descs.append(
                    f"调用工具: {fn.get('name')}, 参数: {fn.get('arguments')}"
                )
            history_text = "\n".join(
                part
                for part in (str(content or "").strip(), "\n".join(tool_descs))
                if part
            )
            plain_messages.append({"role": "assistant", "content": history_text})
            continue

        if isinstance(content, str):
            plain_messages.append({"role": role, "content": content})
            if role == "user" and content.strip():
                chat_info = content
        elif isinstance(content, list):
            text_parts: List[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    text_parts.append(part.get("text", ""))
                elif ptype in ("image_url", "input_image"):
                    iu = part.get("image_url")
                    if isinstance(iu, dict):
                        url = iu.get("url", "")
                    else:
                        url = iu if isinstance(iu, str) else part.get("url", "")
                    if url:
                        image_refs.append(url)
                elif ptype == "file":
                    f = part.get("file") or {}
                    fd = f.get("file_data", "") or ""
                    fname = f.get("filename") or "document.pdf"
                    if fd.startswith("data:"):
                        try:
                            header, b64 = fd.split(",", 1)
                            mime = (
                                header.split(":", 1)[1].split(";", 1)[0]
                                or "application/pdf"
                            )
                            raw = base64.b64decode(b64)
                            file_items.append((fname, raw, mime))
                        except Exception as e:
                            print(
                                f"[Warn] Failed to decode file part: {e}",
                                file=sys.stderr,
                            )
            text = "\n".join([t for t in text_parts if t])
            plain_messages.append({"role": role, "content": text})
            if role == "user" and text.strip():
                chat_info = text

    return chat_info, plain_messages, image_refs, file_items


def upload_image_to_genai(image_ref: str) -> Tuple[str, int, int]:
    """将图片 (data URL 或 http URL) 转存到上科大图片服务器，返回 (静态URL, width, height)"""
    if image_ref.startswith("data:"):
        header, b64 = image_ref.split(",", 1)
        mime = header.split(":", 1)[1].split(";", 1)[0] or "image/png"
        raw = base64.b64decode(b64)
    else:
        current_url = image_ref
        for _ in range(6):
            validate_remote_url(current_url)
            r = httpx.get(
                current_url, verify=False, timeout=30.0, follow_redirects=False
            )
            if r.is_redirect:
                location = r.headers.get("location")
                if not location:
                    r.raise_for_status()
                current_url = str(r.url.join(location))
                continue
            r.raise_for_status()
            break
        else:
            raise ValueError("too many image URL redirects")
        raw = r.content
        mime = (r.headers.get("content-type") or "image/png").split(";")[0].strip()

    if len(raw) > MAX_REMOTE_IMAGE_BYTES:
        raise ValueError(f"image exceeds {MAX_REMOTE_IMAGE_BYTES} byte limit")

    ext = MIME_EXT_MAP.get(mime.lower(), ".png")
    filename = f"api_{uuid.uuid4().hex[:12]}{ext}"

    headers = {
        "Accept": "*/*",
        "token": PIC_UPLOAD_TOKEN,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    }
    files = {"file": (filename, raw, mime)}
    data = {"biz": "temp", "uploadType": "local"}
    r = httpx.post(
        f"{PIC_BASE}/sys/common/upload",
        files=files,
        data=data,
        headers=headers,
        verify=False,
        timeout=30.0,
    )
    r.raise_for_status()
    j = r.json()
    result = j.get("result") or {}
    rel_path = result.get("url") or j.get("message")
    if not rel_path:
        raise RuntimeError(f"Upload failed, unexpected response: {r.text[:200]}")

    static_url = f"{PIC_BASE}/sys/common/static/{rel_path}"
    width = int(result.get("width") or 0)
    height = int(result.get("height") or 0)
    if (not width or not height) and PIL_AVAILABLE:
        try:
            with PILImage.open(io.BytesIO(raw)) as im:
                width, height = im.size
        except Exception:
            pass
    return static_url, width, height


def upload_document_to_genai(
    filename: str, raw: bytes, mime: str, chat_group_id: str, ai_type: str
) -> str:
    """上传 PDF 等文档，返回 fileId"""
    headers = {
        "X-Access-Token": get_shanghaitech_token(),
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    }
    files = {"file": (filename, raw, mime)}
    data = {"biz": "temp"}
    r = httpx.post(
        f"{SHANGHAITECH_BASE_URL}/chat/upload/chat/file",
        params={"chatGroupId": chat_group_id, "aiType": ai_type},
        files=files,
        data=data,
        headers=headers,
        verify=False,
        timeout=60.0,
    )
    r.raise_for_status()
    j = r.json()
    result = j.get("result") or {}
    file_id = result.get("fileId")
    if not file_id:
        raise RuntimeError(f"Document upload failed: {r.text[:200]}")
    return file_id


# ---------------- Chat Completions ----------------


def call_upstream_stream(payload: Dict[str, Any]) -> Generator[dict, None, None]:
    """请求上科大 SSE 并产出解析后的 JSON 事件。

    超时策略: 分段超时——连接 15s，读 600s（大 prompt + 本地 GPU 慢首字可长达数分钟），
    写/池 60s。首字到达前的瞬时网络错误（读超时/连接错误）自动重试最多 2 次；
    一旦开始产出内容则不再重试（无法续传，避免内容重复）。
    """
    headers = get_headers()
    stream_timeout = httpx.Timeout(
        connect=15.0, read=600.0, write=60.0, pool=60.0
    )
    max_ttft_retries = 2
    last_error: Optional[BaseException] = None

    for attempt in range(1 + max_ttft_retries):
        got_any = False
        try:
            with httpx.Client(verify=False, timeout=stream_timeout) as client:
                with client.stream(
                    "POST",
                    f"{SHANGHAITECH_BASE_URL}/chat/start/chat",
                    json=payload,
                    headers=headers,
                ) as resp:
                    ctype = (resp.headers.get("content-type") or "").lower()
                    if "text/event-stream" not in ctype:
                        snippet = resp.read().decode(
                            "utf-8", errors="ignore"
                        )[:150].strip()
                        raise RuntimeError(
                            f"上游返回非流式响应，登录凭据可能已失效，请重新登录并更新 token.txt。上游内容: {snippet}"
                        )
                    saw_choices = False
                    for line in resp.iter_lines():
                        if not line:
                            continue
                        if line.startswith("data:"):
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                if not saw_choices:
                                    raise RuntimeError(
                                        "upstream returned no chat choices; refresh token or check model availability"
                                    )
                                return
                            try:
                                parsed = json.loads(data_str)
                            except Exception:
                                continue
                            if parsed.get("error"):
                                error = parsed["error"]
                                message = (
                                    error.get("message")
                                    if isinstance(error, dict)
                                    else str(error)
                                )
                                raise RuntimeError(message or "upstream returned an error")
                            if isinstance(parsed.get("code"), int) and parsed["code"] >= 400:
                                raise RuntimeError(
                                    parsed.get("errMsg")
                                    or parsed.get("message")
                                    or f"upstream error {parsed['code']}"
                                )
                            if "other" in parsed and "choices" not in parsed:
                                continue
                            if parsed.get("choices"):
                                saw_choices = True
                                got_any = True
                            yield parsed
                    if not saw_choices:
                        raise RuntimeError(
                            "upstream returned no chat choices; refresh token or check model availability"
                        )
                    return
        except (
            httpx.ReadTimeout,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.RemoteProtocolError,
        ) as exc:
            last_error = exc
            if got_any or attempt >= max_ttft_retries:
                _log_ts(
                    f"[stream] 上游流{'中断(已产出内容,无法续传)' if got_any else '首字前失败'}: "
                    f"{type(exc).__name__}: {str(exc)[:120]}"
                    + (f"（第 {attempt + 1} 次尝试）" if got_any is False else "")
                )
                raise
            _log_ts(
                f"[stream] 首字前网络错误（{type(exc).__name__}），自动重试 "
                f"{attempt + 1}/{max_ttft_retries}…"
            )
            time.sleep(1.5)
    if last_error:
        raise last_error


def call_upstream_json(
    payload: Dict[str, Any], timeout: float = 300.0
) -> Dict[str, Any]:
    """调用网页端非流式接口（文生图等），并校验统一响应。"""
    with httpx.Client(verify=False, timeout=timeout) as client:
        resp = client.post(
            f"{SHANGHAITECH_BASE_URL}/chat/start/chat",
            json=payload,
            headers=get_headers(),
        )
        resp.raise_for_status()
        result = resp.json()
    if result.get("success") is False:
        raise RuntimeError(
            result.get("errMsg") or result.get("message") or "upstream request failed"
        )
    return result


TOOL_REFUSAL_RE = re.compile(
    r"(?:tool(?:s| use| execution)? (?:is|are|was|were)?\s*(?:disabled|unavailable)|"
    r"can(?:not|'t) (?:use|invoke|access).{0,30}tool|"
    r"工具.{0,12}(?:禁用|不可用|无法使用|不能调用)|无法.{0,12}(?:调用|使用).{0,8}工具)",
    re.IGNORECASE | re.DOTALL,
)


def is_tool_refusal(text: str) -> bool:
    return bool(text and TOOL_REFUSAL_RE.search(text))


def forced_tool_routing_payload(
    payload: Dict[str, Any], tools: List[Dict[str, Any]], user_text: str
) -> Dict[str, Any]:
    retried = dict(payload)
    protocol = build_tools_system_prompt(tools, "required", routing_only=True)
    retried["chatInfo"] = f"{protocol}\n\n<user_request>\n{user_text}\n</user_request>"
    return retried


# ---------------- Images / Audio ----------------


@app.route("/v1/images/generations", methods=["POST"])
def image_generations():
    req = request.get_json(force=True, silent=True) or {}
    prompt = req.get("prompt")
    model_name = req.get("model", "GPT-Image-2")
    if not isinstance(prompt, str) or not prompt.strip():
        return jsonify(
            {
                "error": {
                    "message": "prompt must be a non-empty string",
                    "type": "invalid_request_error",
                    "param": "prompt",
                    "code": None,
                }
            }
        ), 400
    model_config = find_model_config(model_name) if isinstance(model_name, str) else {}
    if not model_config or str(model_config.get("status")) != "2":
        return jsonify(
            {
                "error": {
                    "message": f"The model '{model_name}' is not an image generation model",
                    "type": "invalid_request_error",
                    "param": "model",
                    "code": None,
                }
            }
        ), 400
    try:
        count = int(req.get("n", 1))
    except (TypeError, ValueError):
        count = 0
    if count < 1 or count > 10:
        return jsonify(
            {
                "error": {
                    "message": "n must be between 1 and 10",
                    "type": "invalid_request_error",
                    "param": "n",
                    "code": None,
                }
            }
        ), 400
    size = req.get("size", "1024x1024")
    if not isinstance(size, str) or not re.fullmatch(r"\d{2,5}x\d{2,5}", size):
        return jsonify(
            {
                "error": {
                    "message": "size must use WIDTHxHEIGHT format",
                    "type": "invalid_request_error",
                    "param": "size",
                    "code": None,
                }
            }
        ), 400
    payload = {
        "chatInfo": prompt.strip(),
        "type": "3",
        "aiType": model_config["aiType"],
        "aiSecType": "1",
        "chatGroupId": uuid.uuid4().hex,
        "rootAiType": model_config.get("rootAiType", "azure"),
        "size": size,
        "quality": req.get("quality", "low"),
        "style": req.get("style", "vivid"),
        "count": count,
        "outputFormat": "png",
        "outputCompression": None,
        "imageBase64": None,
    }
    try:
        result = call_upstream_json(payload)
        items = result.get("data") or []
        data = []
        for item in items:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            url = item["url"]
            if not url.startswith(("http://", "https://", "data:")):
                url = f"{PIC_BASE}/sys/common/static/{url.lstrip('/')}"
            data.append({"url": url})
        if not data:
            raise RuntimeError("upstream returned no image URL")
        return jsonify({"created": int(time.time()), "data": data})
    except Exception as ex:
        return jsonify(
            {
                "error": {
                    "message": f"Image generation failed: {ex}",
                    "type": "server_error",
                }
            }
        ), 502


@app.route("/v1/audio/speech", methods=["POST"])
def audio_speech():
    req = request.get_json(force=True, silent=True) or {}
    text = req.get("input")
    if not isinstance(text, str) or not text.strip():
        return jsonify(
            {
                "error": {
                    "message": "input must be a non-empty string",
                    "type": "invalid_request_error",
                    "param": "input",
                    "code": None,
                }
            }
        ), 400
    if len(text) > 10000:
        return jsonify(
            {
                "error": {
                    "message": "input is too long",
                    "type": "invalid_request_error",
                    "param": "input",
                    "code": None,
                }
            }
        ), 400
    payload = {
        "text": text,
        "speed": req.get("speed", 1),
        "language": req.get("language", "ZH"),
        "speaker_id": req.get("voice", "ZH"),
    }
    try:
        resp = httpx.post(
            f"{SHANGHAITECH_BASE_URL}/chat/convert2wav",
            json=payload,
            headers=get_headers(),
            verify=False,
            timeout=120.0,
        )
        resp.raise_for_status()
        return Response(
            resp.content, content_type=resp.headers.get("content-type", "audio/wav")
        )
    except Exception as ex:
        return jsonify(
            {
                "error": {
                    "message": f"Speech generation failed: {ex}",
                    "type": "server_error",
                }
            }
        ), 502


@app.route("/v1/audio/transcriptions", methods=["POST"])
def audio_transcriptions():
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify(
            {
                "error": {
                    "message": "file is required",
                    "type": "invalid_request_error",
                    "param": "file",
                    "code": None,
                }
            }
        ), 400
    raw = upload.read(MAX_REMOTE_IMAGE_BYTES + 1)
    if len(raw) > MAX_REMOTE_IMAGE_BYTES:
        return jsonify(
            {
                "error": {
                    "message": "audio file is too large",
                    "type": "invalid_request_error",
                    "param": "file",
                    "code": None,
                }
            }
        ), 400
    try:
        resp = httpx.post(
            f"{SHANGHAITECH_BASE_URL}/chat/recognize",
            files={
                "file": (
                    upload.filename,
                    raw,
                    upload.mimetype or "application/octet-stream",
                )
            },
            headers={"X-Access-Token": get_shanghaitech_token()},
            verify=False,
            timeout=180.0,
        )
        resp.raise_for_status()
        result = resp.json().get("result") or {}
        transcription = result.get("transcription", "")
        if isinstance(transcription, str):
            try:
                decoded = json.loads(transcription)
                text = decoded.get("transcription", [{}])[0].get("text", "")
            except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
                text = transcription
        else:
            text = ""
        return jsonify({"text": text})
    except Exception as ex:
        return jsonify(
            {
                "error": {
                    "message": f"Transcription failed: {ex}",
                    "type": "server_error",
                }
            }
        ), 502


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    """标准 OpenAI Chat Completions 接口（支持多模态）"""
    req_data = request.get_json(force=True, silent=True) or {}

    messages = req_data.get("messages", [])
    model_name = req_data.get("model", "gpt-6-astra")
    stream = req_data.get("stream", False)
    max_tokens = req_data.get("max_tokens", 4096)

    def invalid(message: str, param: str):
        return jsonify(
            {
                "error": {
                    "message": message,
                    "type": "invalid_request_error",
                    "param": param,
                    "code": None,
                }
            }
        ), 400

    if not isinstance(model_name, str) or not model_name.strip():
        return invalid("model must be a non-empty string", "model")
    if (
        not isinstance(messages, list)
        or not messages
        or not all(isinstance(m, dict) for m in messages)
    ):
        return invalid("messages must be a non-empty array of objects", "messages")
    if not isinstance(stream, bool):
        return invalid("stream must be a boolean", "stream")
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or max_tokens <= 0
    ):
        return invalid("max_tokens must be a positive integer", "max_tokens")
    raw_tools = req_data.get("tools") or req_data.get("functions") or []
    if not isinstance(raw_tools, list):
        return invalid("tools/functions must be an array", "tools")
    for item in raw_tools:
        function = item.get("function") if isinstance(item, dict) else None
        if req_data.get("tools") is None and isinstance(item, dict):
            function = item
        if (
            not isinstance(function, dict)
            or not isinstance(function.get("name"), str)
            or not function["name"]
        ):
            return invalid("each tool must contain a non-empty function.name", "tools")
    tool_choice_value = req_data.get(
        "tool_choice", req_data.get("function_call", "auto")
    )
    if not isinstance(tool_choice_value, (str, dict)):
        return invalid(
            "tool_choice/function_call must be a string or object", "tool_choice"
        )

    # 请求摘要日志：用于诊断客户端是否传入了工具定义
    n_tools = len(req_data.get("tools") or req_data.get("functions") or [])
    print(
        f"[req] {time.strftime('%H:%M:%S')} model={model_name} stream={stream} msgs={len(messages)} tools={n_tools}",
        file=sys.stderr,
    )

    chat_info, plain_messages, image_refs, file_items = extract_multimodal_parts(
        messages
    )

    model_config = find_model_config(model_name)
    if not model_config:
        return invalid(f"The model '{model_name}' does not exist", "model")
    model_status = str(model_config.get("status", "1"))
    if model_status == "2":
        return invalid(
            f"The model '{model_name}' only supports image generation", "model"
        )
    if (image_refs or file_items) and model_status != "3":
        return invalid(
            f"The model '{model_name}' does not support image/document input",
            "messages",
        )
    chat_group_id = uuid.uuid4().hex
    ai_type = model_config.get("aiType", model_name)

    # 转存图片 → imageUrl / imageUrls
    image_urls: List[str] = []
    img_w = img_h = 0
    for ref in image_refs[:8]:
        try:
            static_url, w, h = upload_image_to_genai(ref)
            image_urls.append(static_url)
            if not img_w:
                img_w, img_h = w, h
        except Exception as e:
            print(f"[Warn] Image upload failed: {e}", file=sys.stderr)

    # 上传文档 → fileIds
    file_ids: List[str] = []
    for fname, raw, mime in file_items[:5]:
        try:
            fid = upload_document_to_genai(fname, raw, mime, chat_group_id, ai_type)
            file_ids.append(fid)
        except Exception as e:
            print(f"[Warn] Document upload failed for {fname}: {e}", file=sys.stderr)

    if not chat_info:
        if image_urls:
            chat_info = "请描述这张图片"
        elif file_ids:
            chat_info = "请阅读这份文档"
        else:
            chat_info = "你好"
    original_chat_info = chat_info

    # 工具调用（Tools / Function Calling）支持
    # 注意：学校后端会忽略 role=system 的消息，工具指令必须拼进 chatInfo（核心字段）才能被模型看到
    tools = req_data.get("tools") or []
    if not tools and req_data.get("functions"):
        # 兼容旧版 functions 参数
        tools = [
            {"type": "function", "function": f} for f in req_data.get("functions", [])
        ]
    tool_choice = req_data.get("tool_choice", req_data.get("function_call", "auto"))
    if tool_choice == "none":
        tools = []
    if tools and TOOL_ENGINE_AVAILABLE:
        # 协议必须位于用户任务之前。Kimi 等模型会拒绝末尾追加的“假装调用工具”指令，
        # 但能可靠地遵循“序列化请求，由外部程序执行”的明确协议。
        protocol = build_tools_system_prompt(
            tools, tool_choice, routing_only="kimi" in model_name.lower()
        )
        chat_info = f"{protocol}\n\n<user_request>\n{chat_info}\n</user_request>"

    # 深度思考控制（严格对齐网页端）
    # 网页端逻辑：默认所有模型 deepThink = False（普通直出模式）
    # 只有当该模型具备思考能力（enableDeepThink == 1），且用户显式要求深度思考时，才传 thinking = True！
    user_wants_thinking = (
        req_data.get("thinking") is True
        or req_data.get("enable_thinking") is True
        or req_data.get("deep_think") is True
        or req_data.get("reasoning_effort") in ["medium", "high"]
        or "-think" in model_name.lower()
    )
    is_model_thinking_capable = str(model_config.get("enableDeepThink")) == "1"
    # 对于 Kimi-k3、Qwen、ChatGLM 等网页端没有思考按钮的模型，绝对不启用 thinking！
    should_think = is_model_thinking_capable and user_wants_thinking

    payload = {
        "chatInfo": chat_info,
        "messages": plain_messages,
        "type": "3",
        "stream": True,
        "aiType": ai_type,
        "aiSecType": "1",
        "chatGroupId": chat_group_id,
        "rootAiType": model_config.get("rootAiType", "azure"),
        "maxToken": max_tokens,
        "netGo": bool(req_data.get("net_search", False)),
    }
    if is_model_thinking_capable:
        payload["thinking"] = should_think
    if image_urls:
        payload["imageUrl"] = image_urls[0]
        payload["imageUrls"] = image_urls
        payload["width"] = img_w
        payload["height"] = img_h
    if file_ids:
        payload["fileIds"] = file_ids

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created_time = int(time.time())
    upstream_model = model_name

    if stream:

        def generate() -> Generator[str, None, None]:
            t0 = time.time()
            accumulated_text = ""
            streamed_chars = 0
            is_tool_stream = bool(tools and "kimi" in model_name.lower())
            _log_ts(f"[stream] 开始: model={model_name} stream=True tools={len(tools or [])}")
            if TOKEN_AUTO_REFRESH_AVAILABLE:
                token_auto_refresh.stream_started()
            try:
                # Kimi 的工具响应先缓冲完整输出，以拦截上游的“内置工具禁用”拒绝语。
                pending_tail = ""  # 尾部保留区：防止探针词被 chunk 边界拆开漏检
                HOLD = 15
                for parsed in call_upstream_stream(payload):
                    choices = parsed.get("choices", [])
                    if choices:
                        ch0 = choices[0]
                        delta = ch0.get("delta")
                        if not isinstance(delta, dict):
                            delta = {}
                            ch0["delta"] = (
                                delta  # 关键：保证 delta 是 parsed 内的真引用，修改才能随序列化发出
                            )
                        # 未显式要求思考时，剔除学校模型自带的内置思维链，保持客户端界面干净
                        if not should_think:
                            delta.pop("reasoning_content", None)
                        content_piece = delta.get("content", "") or ""
                        streamed_chars += len(content_piece)
                        full_piece = pending_tail + content_piece
                        pending_tail = ""

                        if is_tool_stream:
                            # 缓冲期：全部累积，等待流结束统一解析
                            accumulated_text += full_piece
                            if (
                                '"tool_calls"' not in accumulated_text
                                and accumulated_text.count("```") >= 2
                            ):
                                # 确认是普通代码块：整块放行，恢复流式
                                text_chunk = {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_time,
                                    "model": upstream_model or model_name,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {"content": accumulated_text},
                                            "finish_reason": None,
                                        }
                                    ],
                                }
                                yield f"data: {json.dumps(text_chunk, ensure_ascii=False)}\n\n"
                                accumulated_text = ""
                                is_tool_stream = False
                            continue

                        # 探针检测（在拼接后的完整文本上，杜绝拆分漏检）
                        if (
                            "<|open|>" in full_piece
                            or "<tool_" in full_piece
                            or "```" in full_piece
                            or '"tool_calls"' in full_piece
                            or '{"tool_calls' in full_piece
                        ):
                            is_tool_stream = True
                            accumulated_text += full_piece
                            continue

                        # 安全转发：扣除尾部 HOLD 字符防止探针词被拆开；携带 finish_reason 的 chunk 必须立即转发
                        if TOOL_ENGINE_AVAILABLE and full_piece:
                            cleaned = clean_leak_tokens(full_piece)
                            if cleaned != full_piece:
                                delta["content"] = cleaned
                                full_piece = cleaned
                        finish_now = choices[0].get("finish_reason")
                        if finish_now:
                            # 结束 chunk：合并尾部保留区立即转发，确保 finish_reason 不丢
                            merged = pending_tail + full_piece
                            if merged:
                                delta["content"] = merged
                            pending_tail = ""
                            try:
                                yield f"data: {json.dumps(parsed, ensure_ascii=False)}\n\n"
                            except Exception:
                                continue
                        elif len(full_piece) > HOLD:
                            # 足够长：转发扣除尾部保留区之外的部分
                            pending_tail = full_piece[-HOLD:]
                            delta["content"] = full_piece[:-HOLD]
                            try:
                                yield f"data: {json.dumps(parsed, ensure_ascii=False)}\n\n"
                            except Exception:
                                continue
                        else:
                            # 普通短 chunk：扣留不转发，与后续 chunk 拼接后再探针
                            pending_tail = full_piece

                # 流结束：冲刷尾部保留区
                if pending_tail and not is_tool_stream:
                    tail_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": upstream_model or model_name,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "content": clean_leak_tokens(pending_tail)
                                    if TOOL_ENGINE_AVAILABLE
                                    else pending_tail
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(tail_chunk, ensure_ascii=False)}\n\n"

                # 如果是流式工具调用，在结尾发送标准的 tool_calls 结构
                if is_tool_stream and TOOL_ENGINE_AVAILABLE:
                    if is_tool_refusal(accumulated_text):
                        accumulated_text = ""
                        retry_payload = forced_tool_routing_payload(
                            payload, tools, original_chat_info
                        )
                        for retry_event in call_upstream_stream(retry_payload):
                            retry_choices = retry_event.get("choices", [])
                            if retry_choices:
                                retry_delta = retry_choices[0].get("delta", {}) or {}
                                accumulated_text += retry_delta.get("content", "") or ""
                    clean_text, tool_calls = extract_tool_calls(accumulated_text)
                    if tool_calls:
                        # 先发模型的前置说明文字（如有），再发工具调用
                        if clean_text:
                            pre_chunk = {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created_time,
                                "model": upstream_model or model_name,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {
                                            "role": "assistant",
                                            "content": clean_text,
                                        },
                                        "finish_reason": None,
                                    }
                                ],
                            }
                            yield f"data: {json.dumps(pre_chunk, ensure_ascii=False)}\n\n"
                        indexed_calls = []
                        for i, tc in enumerate(tool_calls):
                            tc2 = dict(tc)
                            tc2["index"] = i
                            indexed_calls.append(tc2)
                        tool_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": upstream_model or model_name,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "role": "assistant",
                                        "tool_calls": indexed_calls,
                                    },
                                    "finish_reason": "tool_calls",
                                }
                            ],
                        }
                        yield f"data: {json.dumps(tool_chunk, ensure_ascii=False)}\n\n"
                    elif clean_text:
                        text_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": upstream_model or model_name,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": clean_text},
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                        yield f"data: {json.dumps(text_chunk, ensure_ascii=False)}\n\n"

                yield "data: [DONE]\n\n"
                _log_ts(
                    f"[stream] 完成: model={model_name} 用时 {time.time() - t0:.1f}s"
                    f" 输出 {streamed_chars} 字 tool_stream={is_tool_stream}"
                )
                if TOKEN_AUTO_REFRESH_AVAILABLE:
                    token_auto_refresh.stream_ended()
            except GeneratorExit:
                _log_ts(
                    f"[stream] 客户端中断: model={model_name} 用时 {time.time() - t0:.1f}s"
                    f" 已输出 {streamed_chars} 字"
                )
                if TOKEN_AUTO_REFRESH_AVAILABLE:
                    token_auto_refresh.stream_ended()
                raise
            except Exception as ex:
                _log_ts(
                    f"[stream] 上游错误: model={model_name} "
                    f"{type(ex).__name__}: {str(ex)[:150]}"
                )
                if TOKEN_AUTO_REFRESH_AVAILABLE:
                    token_auto_refresh.stream_ended()
                err_chunk = {
                    "error": {
                        "message": f"Proxy upstream error: {str(ex)}",
                        "type": "server_error",
                    }
                }
                yield f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

        return Response(generate(), mimetype="text/event-stream")

    else:
        full_content = ""
        thinking_content = ""
        finish_reason = "stop"
        usage_info = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        try:
            for parsed in call_upstream_stream(payload):
                if tools and os.environ.get("PROXY_DEBUG_TOOLS") == "1":
                    print(f"[tool-debug] event={parsed!r}", file=sys.stderr)
                choices = parsed.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {}) or {}
                    chunk_text = delta.get("content", "") or ""
                    if chunk_text:
                        full_content += chunk_text
                    think_text = delta.get("reasoning_content", "") or ""
                    if think_text:
                        thinking_content += think_text
                    if choices[0].get("finish_reason"):
                        finish_reason = choices[0].get("finish_reason")
                if parsed.get("model"):
                    upstream_model = parsed.get("model")
                if parsed.get("usage"):
                    usage_info = parsed.get("usage")
        except Exception as ex:
            return jsonify(
                {
                    "error": {
                        "message": f"Upstream connection failed: {str(ex)}",
                        "type": "server_error",
                    }
                }
            ), 502

        if tools and "kimi" in model_name.lower() and is_tool_refusal(full_content):
            full_content = ""
            thinking_content = ""
            finish_reason = "stop"
            try:
                retry_payload = forced_tool_routing_payload(
                    payload, tools, original_chat_info
                )
                for parsed in call_upstream_stream(retry_payload):
                    choices = parsed.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {}) or {}
                        full_content += delta.get("content", "") or ""
                        thinking_content += delta.get("reasoning_content", "") or ""
                        if choices[0].get("finish_reason"):
                            finish_reason = choices[0]["finish_reason"]
                    upstream_model = parsed.get("model") or upstream_model
                    usage_info = parsed.get("usage") or usage_info
            except Exception as ex:
                return jsonify(
                    {
                        "error": {
                            "message": f"Tool routing retry failed: {ex}",
                            "type": "server_error",
                        }
                    }
                ), 502

        # 工具调用解析与特殊标记清洗
        tool_calls = []
        if TOOL_ENGINE_AVAILABLE and tools:
            if os.environ.get("PROXY_DEBUG_TOOLS") == "1":
                print(f"[tool-debug] raw={full_content!r}", file=sys.stderr)
            clean_text, tool_calls = extract_tool_calls(full_content)
            if tool_calls:
                full_content = clean_text or None
                finish_reason = "tool_calls"
            else:
                full_content = clean_leak_tokens(full_content)
        elif TOOL_ENGINE_AVAILABLE:
            # 即使客户端未显式提供 tools，也清理可能漏出的开源模型原生私有标记
            full_content = clean_leak_tokens(full_content)

        message_obj = {"role": "assistant", "content": full_content}
        if tool_calls:
            message_obj["tool_calls"] = tool_calls
        # 仅当用户显式要求思考时才透传思维链；否则静音（学校部分模型会自带内置思考过程）
        if thinking_content and should_think:
            message_obj["reasoning_content"] = thinking_content

        return jsonify(
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": created_time,
                "model": upstream_model or model_name,
                "choices": [
                    {"index": 0, "message": message_obj, "finish_reason": finish_reason}
                ],
                "usage": usage_info,
            }
        )


# ---------------- 启动 ----------------


def get_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def print_banner(actual_port: int):
    models = fetch_shanghaitech_models()
    lan_ip = get_lan_ip()
    line = "=" * 64
    print(line)
    print("   上海科技大学 GenAI -> OpenAI 兼容 API 代理")
    print(line)
    print(f"   接口地址 (本机)  : http://127.0.0.1:{actual_port}/v1")
    print(f"   接口地址 (局域网): http://{lan_ip}:{actual_port}/v1")
    print(f"   API Key          : {LOCAL_API_KEY or '(已关闭鉴权)'}")
    print(f"   登录凭据来源     : {token_source()}")
    auth_status = upstream_auth_status()
    expiry = get_token_expiry()
    if expiry:
        expiry_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expiry))
        print(f"   登录凭据状态     : {auth_status}（本地时间 {expiry_text} 到期）")
    else:
        print(f"   登录凭据状态     : {auth_status}")
    print("-" * 64)
    if models:
        print(f'   可用模型 ({len(models)} 个)，填入客户端"模型名"即可调用:')
        for m in models:
            display = (m.get("aiName") or "").strip() or m.get("aiType", "?")
            vendor = m.get("rootAiType", "?")
            vision = "识图:是" if str(m.get("status")) == "3" else "识图:否"
            think = " 思考:按需" if str(m.get("enableDeepThink")) == "1" else ""
            print(f"     - {display:<26} [{vendor:<10}] {vision}{think}")
        print()
        print("   说明: 模型名与网页端显示一致；内部 ID（如 deepseek-pro）也可用。")
        print("         所有模型默认极速直出；带\"思考:按需\"的模型，")
        print("         仅当客户端显式传入 thinking/effort 参数时才启用深度思考。")
    else:
        print("   [!] 暂时无法获取模型列表（Token 未配置 / 失效，或网络问题）")
        print("       请运行 get_token.py 获取并保存 Token 后重启本程序。")
    print(line)
    print("   按 Ctrl + C 停止服务")
    print(line)


if __name__ == "__main__":
    import warnings
    from urllib3.exceptions import InsecureRequestWarning

    warnings.simplefilter("ignore", InsecureRequestWarning)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    # 1. 动态检测并解析实际可用端口，防止冲突
    requested_port = parse_requested_port()
    if is_existing_proxy(requested_port):
        print("=" * 64)
        print("   上海科技大学 GenAI -> OpenAI 兼容 API 代理")
        print("=" * 64)
        print(f"   服务已经在运行，无需重复启动：http://127.0.0.1:{requested_port}/v1")
        print("   如需重启，请先关闭原来的代理窗口，再运行 start.bat。")
        print("=" * 64)
        sys.exit(0)
    actual_port = resolve_actual_port(requested_port)

    # 2. 只有真实端口完全确定后，才打印 Banner，保证界面显示的地址与实际运行绝对一致！
    print_banner(actual_port=actual_port)

    # 2.5 启动凭据自动刷新后台服务（Token 过期/临近过期时自动续期）
    if TOKEN_AUTO_REFRESH_AVAILABLE:
        token_auto_refresh.start_background()
    else:
        print("   [token] 自动刷新模块不可用（token_auto_refresh.py 缺失或依赖不足）。")

    # 3. 绑定并启动生产级 WSGI 服务器
    if WAITRESS_AVAILABLE:
        print(
            f"   [server] 生产级 WSGI 服务器 waitress 已启动，已成功监听端口 {actual_port}"
        )
        waitress_serve(
            app, host="0.0.0.0", port=actual_port, threads=32, channel_timeout=300
        )
    else:
        print(
            f"   [server] 未安装 waitress，使用 Flask 内置服务器，已监听端口 {actual_port}"
        )
        import logging

        log = logging.getLogger("werkzeug")
        log.setLevel(logging.ERROR)
        app.run(host="0.0.0.0", port=actual_port, debug=False, threaded=True)
