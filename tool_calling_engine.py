# -*- coding: utf-8 -*-
"""工具调用协议与解析引擎。

核心协议（XML 工具定义 + few-shot 示例）与解析链（DSML 归一化、坏 JSON
多重修复、兜底提取器、白名单过滤）改编自 MIT 许可的同类项目
jollyxenon/shanghaitech-genai2api（https://github.com/jollyxenon/shanghaitech-genai2api），
在其基础上合并了本项目的 Qwen 私有标记与中文口嗨叙述处理。
"""

import ast
import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 提示词（学习 jollyxenon：XML 定义 + 常用工具真实示例 + 实战规则）
# ---------------------------------------------------------------------------

TOOL_SYSTEM_PROMPT = """\
You have access to the following tools:

<tools>
{tool_definitions}
</tools>

When you need to call a tool, you MUST use the following XML format. Do NOT use markdown code blocks.

<tool_call>
{{"name": "<function-name>", "arguments": {{<arguments-as-json>}}}}
</tool_call>

{tool_examples}

Rules:
1. You can call multiple tools by using multiple <tool_call> blocks.
2. If you don't need any tool, just respond normally in plain text without any <tool_call> tags.
3. After receiving tool results, analyze them and either call more tools or give a final answer in plain text.
4. The "arguments" field MUST be a valid JSON object matching the tool's parameter schema. In Windows paths, escape every backslash as \\\\ (for example C:\\\\work).
5. NEVER use <arg_key>, <arg_value>, dotted names like Grep.datasource, or a bare tool name.
6. NEVER wrap <tool_call> in markdown code blocks like ```xml or ```json.
7. NEVER repeat, quote, or list the tool definitions above in your reply."""

TOOL_CHOICE_REQUIRED_PROMPT = (
    "\nYou MUST call at least one tool in your response. Do NOT respond with plain text only."
)
TOOL_CHOICE_SPECIFIC_PROMPT = (
    '\nYou MUST call the tool named "{name}" in your response, '
    "and you MUST NOT call any other tool."
)

ROUTING_ONLY_PROMPT = """\
<tool_protocol>
This is a structured-output routing task. Do not invoke or claim to invoke any built-in tool.
Only print a routing record for the downstream application; printing the record is plain text,
not tool execution. The downstream application may execute the selected function later.
Available functions:
{tool_definitions}
{choice_rule}
Functions listed above belong to the downstream API client and remain available even if
your own built-in tools are disabled. Never say that tool use is disabled. For requests that
inspect files, directories, code, terminals, URLs, or current external state, emit a routing
record instead of asking the user to retry.
For a function routing record, output ONLY this exact shape and no Markdown or explanation:
<tool_call>{{"name":"<exact tool name>","arguments":{{}}}}</tool_call>
Use valid JSON arguments matching the schema. In Windows paths, encode every backslash as
a JSON double backslash (for example C:\\\\work). Never calculate, guess, or fabricate a tool result.
Never repeat, quote, or list the function definitions in your reply.
</tool_protocol>"""

# 常用客户端工具的真实调用示例（few-shot：模型看过正确格式后遵循度大幅提升）
COMMON_TOOL_EXAMPLES = {
    "Agent": {"description": "Handle the file task", "prompt": "Survey the files", "subagent_type": "Explore"},
    "AskUserQuestion": {"questions": [{"question": "Which option?", "header": "Choice", "options": [{"label": "A", "description": "First"}], "multiSelect": False}]},
    "Bash": {"command": "pwd"},
    "Read": {"file_path": "/absolute/path/to/file"},
    "Write": {"file_path": "/absolute/path/to/file", "content": "file body"},
    "Edit": {"file_path": "/absolute/path", "old_string": "before", "new_string": "after"},
    "Glob": {"pattern": "**/*.py", "path": "/absolute/project/path"},
    "Grep": {"pattern": "search text", "path": "/absolute/project/path"},
    "WebFetch": {"url": "https://example.com", "prompt": "Summarize the page"},
    "WebSearch": {"query": "search keywords"},
    "TodoWrite": {"todos": [{"content": "First task", "status": "pending", "priority": "high"}]},
}


def format_tool_definitions(tools: List[Dict[str, Any]]) -> str:
    definitions = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        func = tool.get("function", {})
        params = func.get("parameters", {})
        params_json = json.dumps(params, ensure_ascii=False, indent=2)
        definitions.append(
            f"<tool_definition>\n"
            f"  <name>{func.get('name', '')}</name>\n"
            f"  <description>{func.get('description', '')}</description>\n"
            f"  <parameters>\n{params_json}\n  </parameters>\n"
            f"</tool_definition>"
        )
    return "\n".join(definitions)


def _format_tool_examples(tools: List[Dict[str, Any]]) -> str:
    examples = []
    for tool in tools:
        name = (tool.get("function") or {}).get("name")
        if name not in COMMON_TOOL_EXAMPLES:
            continue
        call_obj = {"name": name, "arguments": COMMON_TOOL_EXAMPLES[name]}
        examples.append(
            "<tool_call>\n" + json.dumps(call_obj, ensure_ascii=False) + "\n</tool_call>"
        )
    if not examples:
        return ""
    return "Examples of valid tool calls:\n" + "\n".join(examples)


def build_tools_system_prompt(
    tools: List[Dict[str, Any]], tool_choice: Any = "auto", routing_only: bool = False
) -> str:
    """将 OpenAI tools 转成模型容易稳定遵循的 XML 文本协议。"""
    if not tools:
        return ""

    forced_name = None
    if isinstance(tool_choice, dict):
        forced_name = tool_choice.get("name") or (tool_choice.get("function") or {}).get("name")
    required = tool_choice == "required" or bool(forced_name)

    choice_rule = (
        f'You MUST select the tool named "{forced_name}".'
        if forced_name
        else "You MUST select one or more tools; a normal answer is not allowed."
        if required
        else "If no listed tool is needed, answer normally without the envelope."
    )

    if routing_only:
        return ROUTING_ONLY_PROMPT.format(
            tool_definitions=format_tool_definitions(tools),
            choice_rule=choice_rule,
        )

    prompt = TOOL_SYSTEM_PROMPT.format(
        tool_definitions=format_tool_definitions(tools),
        tool_examples=_format_tool_examples(tools),
    )
    if tool_choice == "required":
        prompt += TOOL_CHOICE_REQUIRED_PROMPT
    elif forced_name:
        prompt += TOOL_CHOICE_SPECIFIC_PROMPT.format(name=forced_name)
    return prompt


def inject_tools_prompt(
    messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """将工具定义注入到 messages 中（保留：旧接口兼容）。"""
    if not tools:
        return messages
    prompt = build_tools_system_prompt(tools)
    new_messages = []
    injected = False
    for msg in messages:
        if msg.get("role") == "system" and not injected:
            content = msg.get("content", "")
            new_messages.append({"role": "system", "content": f"{content}\n\n{prompt}" if content else prompt})
            injected = True
        else:
            new_messages.append(dict(msg))
    if not injected:
        new_messages.insert(0, {"role": "system", "content": prompt})
    return new_messages


def allowed_tool_names(tools: List[Dict[str, Any]]) -> Optional[set]:
    """从 tools 提取白名单（防模型幻觉出不存在的工具名）。"""
    names = set()
    for t in tools or []:
        if isinstance(t, dict):
            name = (t.get("function") or {}).get("name") or t.get("name")
            if name:
                names.add(name)
    return names or None


# ---------------------------------------------------------------------------
# 解析链（改编自 jollyxenon/shanghaitech-genai2api tools/parsing.py）
# ---------------------------------------------------------------------------

DSML_OPEN_PREFIX_FULLWIDTH = "<｜DSML｜"
DSML_OPEN_PREFIX_ASCII = "<|DSML|"
TOOL_CALL_OPEN_MARKERS = ("<tool_call", DSML_OPEN_PREFIX_FULLWIDTH, DSML_OPEN_PREFIX_ASCII)
DSML_OPEN_TAG_RE = re.compile(r"<\s*[|｜]\s*DSML\s*[|｜]\s*([A-Za-z_][A-Za-z0-9_]*)?\s*>", re.IGNORECASE)
DSML_CLOSE_TAG_RE = re.compile(r"</\s*[|｜]\s*DSML\s*[|｜]\s*([A-Za-z_][A-Za-z0-9_]*)?\s*>", re.IGNORECASE)
DSML_ATTR_OPEN_TAG_RE = re.compile(
    r"<\s*[|｜]\s*DSML\s*[|｜]\s*name\s*=\s*[\"']([^\"']+)[\"']\s*>", re.IGNORECASE
)
DSML_ANY_TAG_RE = re.compile(r"</?\s*[|｜]\s*DSML\s*[|｜]\s*[^>]*>", re.IGNORECASE)

COMMON_TOOL_ARG_KEYS = (
    "notebook_path", "new_string", "old_string", "file_path", "head_limit",
    "output_mode", "edit_mode", "description", "subagent_type", "command",
    "pattern", "offset", "limit", "path", "query", "prompt", "url",
)
TOOL_ARG_KEYS_BY_TOOL = {
    "Bash": ("command", "description"),
    "Read": ("file_path", "path", "offset", "limit"),
    "Glob": ("pattern", "path"),
    "Grep": ("pattern", "path", "output_mode", "head_limit"),
    "WebFetch": ("url", "prompt"),
    "WebSearch": ("query", "allowed_domains", "blocked_domains"),
    "Agent": ("description", "prompt", "subagent_type"),
}

# 私有标记碎片（Qwen 等开源模型内部 Token）
_LEAK_TOKEN_RE = re.compile(
    r"<\|open\|>|<\|sep\|>|<\|close\|>|<\|endoftext\|>|<\|im_start\|>|<\|im_end\|>"
    r"|<\|tool_calls_begin\|>|<\|tool_calls_end\|>"
    r"|</?message>|</?tool_info>|<dic>[^<]*</dic>"
)


def _is_tool_call_tag_name(name: Optional[str]) -> bool:
    if not name:
        return True
    return name.lower().endswith("call")


def normalize_tool_call_tags(text: str) -> str:
    """把学校模型原生 DSML 工具标记归一化为 <tool_call>。"""
    if not text or "DSML" not in text:
        return text

    def replace_open(match):
        if _is_tool_call_tag_name(match.group(1)):
            return "<tool_call>"
        return match.group(0)

    def replace_close(match):
        if _is_tool_call_tag_name(match.group(1)):
            return "</tool_call>"
        return match.group(0)

    normalized = DSML_OPEN_TAG_RE.sub(replace_open, text)
    normalized = DSML_CLOSE_TAG_RE.sub(replace_close, normalized)
    normalized = DSML_ATTR_OPEN_TAG_RE.sub(r'<tool_call name="\1">', normalized)
    return normalized


def strip_dsml_tags(text: str) -> str:
    if not text or "DSML" not in text:
        return text
    return DSML_ANY_TAG_RE.sub("", text)


def strip_think_blocks(content: str) -> str:
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


def _extract_balanced_brace_segment(text: str, start: int) -> Tuple[Optional[str], Optional[int]]:
    if start < 0 or start >= len(text) or text[start] != "{":
        return None, None
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1], idx + 1
    return None, None


def _extract_json_object(raw: str) -> Optional[dict]:
    start = raw.find("{")
    while start != -1:
        segment, _end = _extract_balanced_brace_segment(raw, start)
        if segment:
            try:
                return json.loads(segment)
            except (json.JSONDecodeError, ValueError):
                pass
        start = raw.find("{", start + 1)
    return None


def _sanitize_json_escapes(raw: str) -> str:
    return re.sub(r'\\(?=[^"\\/bfnrtu])', r"\\\\", raw)


def _load_relaxed_json(raw: str):
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        pass
    sanitized = _sanitize_json_escapes(raw)
    if sanitized != raw:
        try:
            return json.loads(sanitized)
        except (json.JSONDecodeError, ValueError):
            pass
    stripped = sanitized.strip()
    # Python 字面量回退（单引号、True/False/None）
    if stripped[:1] in ("{", "["):
        try:
            literal = ast.literal_eval(stripped)
            if isinstance(literal, (dict, list)):
                return literal
        except (ValueError, SyntaxError):
            pass
    # 未闭合大括号自动补齐
    if not stripped.startswith("{"):
        return None
    depth = 0
    in_string = False
    escape = False
    for ch in stripped:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(depth - 1, 0)
    if depth <= 0:
        return None
    try:
        return json.loads(stripped + ("}" * depth))
    except (json.JSONDecodeError, ValueError):
        return None


def _clean_tool_name(name: str) -> str:
    cleaned = name.strip()
    match = re.search(r"[A-Za-z_][A-Za-z0-9_.-]*", cleaned)
    return match.group(0) if match else cleaned


def _extract_tool_name_from_attrs(attrs: Optional[str]) -> Optional[str]:
    if not attrs:
        return None
    match = re.search(r'\bname\s*=\s*["\']([^"\']+)["\']', attrs)
    return _clean_tool_name(match.group(1)) if match else None


def _normalize_tool_payload(payload, tool_name=None):
    if not isinstance(payload, dict):
        return None
    if "name" in payload:
        name = _clean_tool_name(str(payload["name"]))
        if "arguments" in payload:
            arguments = payload["arguments"]
        else:
            arguments = {k: v for k, v in payload.items() if k != "name"}
        if name == "Bash" and isinstance(arguments, str):
            arguments = {"command": arguments}
        return {"name": name, "arguments": _repair_arguments_paths(arguments)}
    if len(payload) == 1:
        candidate_name, candidate_args = next(iter(payload.items()))
        cleaned = _clean_tool_name(str(candidate_name))
        if cleaned and isinstance(candidate_args, dict):
            return {"name": cleaned, "arguments": candidate_args}
    if tool_name:
        return {"name": tool_name, "arguments": payload}
    return None


def _parse_malformed_named_tool_payload(raw: str, tool_name=None):
    """修复 Bash command 内未转义引号等常见坏 JSON。"""
    normalized = raw.strip()
    name = tool_name
    if not name:
        m = re.search(r'"name"\s*:\s*"([^"]+)"', normalized)
        if m:
            name = _clean_tool_name(m.group(1))
    if not name or '"arguments"' not in normalized:
        return None
    cm = re.search(r'"command"\s*:\s*"', normalized, re.DOTALL)
    if not cm:
        return None
    tail = normalized[cm.end():]
    vm = re.match(r"(?s)(.*)\"\s*\}\s*\}?\s*$", tail)
    if not vm:
        return None
    return {"name": name, "arguments": {"command": vm.group(1).replace('\\"', '"')}}


def _clean_arg_value(value: str) -> str:
    cleaned = value.strip()
    cleaned = re.sub(r"</?tool_call>\s*$", "", cleaned, flags=re.DOTALL).strip()
    cleaned = re.sub(r"</?arg_value>\s*$", "", cleaned, flags=re.DOTALL).strip()
    for suffix in ('"}})', '"}}', '"})', '"}', '">', '"'):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].rstrip()
            break
    return cleaned


def _parse_tool_call_body(raw: str, tool_name=None):
    raw = raw.strip()
    normalized_raw = raw.replace('\\"', '"')

    call = _normalize_tool_payload(_load_relaxed_json(raw), tool_name=tool_name)
    if call:
        return call
    for candidate in (raw, normalized_raw):
        call = _normalize_tool_payload(_extract_json_object(candidate), tool_name=tool_name)
        if call:
            return call
    call = _parse_malformed_named_tool_payload(raw, tool_name=tool_name)
    if call:
        return call
    # 裸工具名 + 参数 JSON
    named_json = re.match(
        r"^([A-Za-z_][A-Za-z0-9_.-]*)\s*\n\s*(\{.*\})\s*$", normalized_raw, re.DOTALL
    )
    if named_json:
        arguments = _load_relaxed_json(named_json.group(2))
        if isinstance(arguments, dict):
            return {"name": _clean_tool_name(named_json.group(1)), "arguments": arguments}
    # 行内 "key": "value" 对
    inline_pairs = re.findall(r'([A-Za-z0-9_]+)"\s*:\s*"([^"]*)"', normalized_raw, re.DOTALL)
    if inline_pairs:
        name = _clean_tool_name(normalized_raw.split("<arg_key>", 1)[0].split("{", 1)[0].strip())
        name = name or tool_name
        if name and not name.startswith('"'):
            arguments = {k.strip(): v.strip() for k, v in inline_pairs if k.strip()}
            if arguments:
                return {"name": name, "arguments": arguments}
    return None


_BS = chr(92)
_CTRL_TO_LITERAL = {
    chr(9): _BS + "t",
    chr(10): _BS + "n",
    chr(13): _BS + "r",
    chr(8): _BS + "b",
    chr(12): _BS + "f",
}


def _repair_windows_paths(value):
    """模型未转义的 Windows 路径：JSON 把 \\t 等解析成了控制字符，检测到盘符特征时还原为字面反斜杠。"""
    if isinstance(value, str) and re.search(r"[A-Za-z]:" + "[" + re.escape("".join(_CTRL_TO_LITERAL)) + "]", value):
        for ctrl, lit in _CTRL_TO_LITERAL.items():
            value = value.replace(ctrl, lit)
    return value


def _repair_arguments_paths(arguments):
    if isinstance(arguments, dict):
        return {k: _repair_windows_paths(v) for k, v in arguments.items()}
    return arguments


def _coerce_tool_arguments(tool_name: str, arguments) -> dict:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        parsed = _load_relaxed_json(arguments)
        if isinstance(parsed, dict):
            return parsed
        if tool_name == "Bash":
            return {"command": arguments}
        return {"arguments": arguments}
    if arguments is None:
        return {}
    return {"arguments": arguments}


def _clean_remaining_text(text: str) -> Optional[str]:
    if not text:
        return None
    cleaned = re.sub(r"<tool_result>.*?</tool_result>", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"<agent-other-thinking>.*?</agent-other-thinking>", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"</?tool_call\b[^>]*>", "", cleaned)
    cleaned = re.sub(r"</?tool_calls\b[^>]*>", "", cleaned)
    cleaned = re.sub(r"</?tool_\d+>", "", cleaned)
    cleaned = strip_dsml_tags(cleaned)
    cleaned = cleaned.replace("</think>", "")
    cleaned = _LEAK_TOKEN_RE.sub("", cleaned)
    cleaned = re.sub(r"^\s*\[\]\s*$", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip()
    return cleaned or None


def _make_call(name: str, arguments) -> Dict[str, Any]:
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def _drop_spans(text: str, spans) -> Optional[str]:
    parts, cursor = [], 0
    for start, end in sorted(spans):
        if start > cursor:
            parts.append(text[cursor:start])
        cursor = max(cursor, end)
    if cursor < len(text):
        parts.append(text[cursor:])
    return _clean_remaining_text("".join(parts))


def _extract_function_style_calls(cleaned: str, allowed):
    matches = list(re.finditer(r"([A-Za-z_][A-Za-z0-9_.-]*)\(\s*\{", cleaned))
    if not matches:
        return [], None
    spans, tool_calls = [], []
    for match in matches:
        tool_name = _clean_tool_name(match.group(1))
        if allowed and tool_name not in allowed:
            continue
        json_text, end_idx = _extract_balanced_brace_segment(cleaned, match.end() - 1)
        if not json_text:
            continue
        payload = _load_relaxed_json(json_text)
        if not isinstance(payload, dict):
            continue
        span_end = end_idx
        while span_end < len(cleaned) and cleaned[span_end].isspace():
            span_end += 1
        if span_end < len(cleaned) and cleaned[span_end] == ")":
            span_end += 1
        spans.append((match.start(), span_end))
        tool_calls.append(_make_call(tool_name, payload))
    if not tool_calls:
        return [], None
    return tool_calls, _drop_spans(cleaned, spans)


def _extract_bare_json_tool_calls(cleaned: str, allowed):
    tool_calls, spans = [], []
    idx = 0
    while idx < len(cleaned):
        start = cleaned.find("{", idx)
        if start == -1:
            break
        json_text, end_idx = _extract_balanced_brace_segment(cleaned, start)
        if not json_text:
            idx = start + 1
            continue
        call = _normalize_tool_payload(_load_relaxed_json(json_text))
        if not call:
            idx = start + 1
            continue
        if allowed and call["name"] not in allowed:
            idx = end_idx
            continue
        spans.append((start, end_idx))
        tool_calls.append(_make_call(call["name"], call.get("arguments", {})))
        idx = end_idx
    if not tool_calls:
        return [], None
    return tool_calls, _drop_spans(cleaned, spans)


def _run_fallback_extractors(cleaned: str, allowed):
    for extractor in (_extract_function_style_calls, _extract_bare_json_tool_calls):
        tool_calls, remaining = extractor(cleaned, allowed)
        if tool_calls:
            return tool_calls, remaining
    return None, None


def _extract_core(content: str, allowed=None):
    """核心提取（jollyxenon 链路）：返回 (tool_calls|None, remaining_text)。"""
    cleaned = normalize_tool_call_tags(strip_think_blocks(content))
    # 围栏中的 <tool_call> 解围
    cleaned = re.sub(
        r"```(?:xml|json|plaintext|text)?\s*\n?\s*(<tool_call\b[^>]*>.*?</tool_call>)\s*\n?\s*```",
        r"\1",
        cleaned,
        flags=re.DOTALL,
    )
    match_pattern = re.compile(r"<tool_call\b([^>]*)>\s*(.*?)\s*</tool_call>", re.DOTALL)
    open_pattern = re.compile(r"<tool_call\b([^>]*)>", re.DOTALL)
    matches = list(match_pattern.finditer(cleaned))
    parsed_list = []
    remaining = None

    if matches:
        for match in matches:
            parsed = _parse_tool_call_body(
                match.group(2), tool_name=_extract_tool_name_from_attrs(match.group(1))
            )
            parsed_list.append(parsed)
    elif "<tool_call" in cleaned:
        openings = list(open_pattern.finditer(cleaned))
        if openings:
            remaining = cleaned[: openings[0].start()].strip()
            for index, opening in enumerate(openings):
                end = openings[index + 1].start() if index + 1 < len(openings) else len(cleaned)
                body = cleaned[opening.end():end]
                body = re.sub(r"</?tool_call\b[^>]*>", "", body).strip()
                if not body:
                    continue
                parsed = _parse_tool_call_body(
                    body, tool_name=_extract_tool_name_from_attrs(opening.group(1))
                )
                parsed_list.append(parsed)

    tool_calls = []
    for parsed in parsed_list:
        if not parsed:
            continue
        if allowed and parsed["name"] not in allowed:
            continue
        tool_calls.append(
            _make_call(parsed["name"], _coerce_tool_arguments(parsed["name"], parsed.get("arguments", {})))
        )

    if not tool_calls:
        fallback_calls, fallback_remaining = _run_fallback_extractors(cleaned, allowed)
        if fallback_calls:
            return fallback_calls, fallback_remaining
        return None, (cleaned if "DSML" in content else content)

    if remaining is None:
        remaining = match_pattern.sub("", cleaned).strip()
    return tool_calls, _clean_remaining_text(remaining)


# ---------------------------------------------------------------------------
# 兼容层：Qwen 私有标记 + 中文口嗨叙述 + 对外统一接口
# ---------------------------------------------------------------------------

def find_narration_spans(text: str):
    """返回所有「调用工具: X, 参数: {...}」叙述的 (start, end) 精确跨度。"""
    spans = []
    for nm in re.finditer(r"调用工具[:：]?\s*([A-Za-z0-9_\.\-]+)", text):
        rest = text[nm.end(): nm.end() + 80]
        pm = re.search(r"参数[:：]?\s*[\(（]?\s*\{", rest)
        if not pm:
            continue
        brace_pos = nm.end() + pm.end() - 1
        try:
            _obj, consumed = json.JSONDecoder().raw_decode(text[brace_pos:])
        except Exception:
            stop = text.find("\n", brace_pos)
            nxt = text.find("调用工具", nm.end())
            candidates = [x for x in (stop if stop != -1 else len(text), nxt if nxt != -1 else len(text)) if x > brace_pos]
            spans.append((nm.start(), min(candidates)))
            continue
        spans.append((nm.start(), brace_pos + consumed))
    return spans


def _extract_qwen_calls(text: str):
    """Qwen 私有标记 <|open|>tools ... <|close|>tools 提取。"""
    if "<|open|>" not in text:
        return None
    calls = []
    for cm in re.finditer(r'<\|open\|>call tool="([^"]+)"[^<]*<\|sep\|>(.*?)<\|close\|>call', text, re.DOTALL):
        args = {}
        for k, v in re.findall(r'<\|open\|>argument key="([^"]+)"[^<]*<\|sep\|>(.*?)<\|close\|>argument', cm.group(2), re.DOTALL):
            try:
                args[k] = json.loads(v.strip())
            except Exception:
                args[k] = v.strip()
        calls.append(_make_call(cm.group(1).strip(), args))
    return calls or None


def _extract_narration_calls(text: str):
    calls = []
    for nm in re.finditer(r"调用工具[:：]?\s*([A-Za-z0-9_\.\-]+)", text):
        rest = text[nm.end(): nm.end() + 80]
        pm = re.search(r"参数[:：]?\s*[\(（]?\s*\{", rest)
        if not pm:
            continue
        brace_pos = nm.end() + pm.end() - 1
        try:
            obj, _consumed = json.JSONDecoder().raw_decode(text[brace_pos:])
        except Exception:
            continue
        if isinstance(obj, dict):
            calls.append(_make_call(nm.group(1), obj))
    return calls or None


def clean_leak_tokens(text: str) -> str:
    if not text:
        return text
    cleaned = _LEAK_TOKEN_RE.sub("", text)
    cleaned = re.sub(r"<tool_call>\s*</tool_call>", "", cleaned)
    return cleaned


def strip_tools_prompt_echo(text: str, tools_prompt: str) -> str:
    """剔除模型回复中复述的注入协议回声（完整协议 / TOOLS 头 / XML/JSON 工具定义）。"""
    if not text or not tools_prompt:
        return text
    out = text.replace(tools_prompt, "")
    m = re.search(r"^\s*#?\s*TOOLS\s*[:：]?\s*$", out, re.MULTILINE)
    if m:
        arr_end = out.find("]", m.end())
        if arr_end != -1:
            out = out[: m.start()] + out[arr_end + 1:]
    # XML 工具定义回声
    out = re.sub(r"<tool_definition>.*?</tool_definition>", "", out, flags=re.DOTALL)
    out = re.sub(r"</?tools>", "", out)
    # JSON 工具定义数组回声（含格式化）
    for am in re.finditer(r"\[\s*\{\s*\"name\"", out):
        start = am.start()
        try:
            obj, consumed = json.JSONDecoder().raw_decode(out[start:])
        except Exception:
            continue
        if isinstance(obj, list) and len(obj) >= 2 and all(isinstance(x, dict) and "name" in x for x in obj[:3]):
            out = out[:start] + out[start + consumed:]
    out = re.sub(r"```[a-zA-Z]*\s*```", "", out)
    return out.strip()


def extract_tool_calls(
    text: str, tools_present: bool = False, allowed_names: Optional[set] = None
) -> Tuple[Optional[str], Optional[List[Dict[str, Any]]]]:
    """对外统一接口：返回 (清理后正文, tool_calls|None)。"""
    if not text:
        return text, None

    # 1) 核心链路：<tool_call>/DSML/兜底
    calls, remaining = _extract_core(text, allowed=allowed_names)
    if calls:
        clean = remaining
        if tools_present and clean:
            for s, e in find_narration_spans(clean):
                clean = clean[:s] + clean[e:]
        return (clean if (clean and clean.strip()) else None), calls

    # 2) Qwen 私有标记
    qwen = _extract_qwen_calls(text)
    if qwen and (not allowed_names or all(c["function"]["name"] in allowed_names for c in qwen)):
        clean = text
        start = clean.find("<|open|>tools")
        if start == -1:
            start = clean.find("<|open|>")
        end = clean.rfind("<|close|>tools")
        end = end + len("<|close|>tools") if end != -1 else len(clean)
        clean = clean_leak_tokens(clean[:start] + clean[end:])
        return (clean if clean.strip() else None), qwen

    # 3) 中文口嗨叙述
    narr = _extract_narration_calls(text)
    if narr and tools_present:
        clean = text
        for s, e in find_narration_spans(clean):
            clean = clean[:s] + clean[e:]
        return (clean if clean.strip() else None), narr

    # 4) 无工具调用：tools_present 时仍剥离可能的叙述残迹
    if tools_present and "调用工具" in text:
        out = text
        for s, e in find_narration_spans(out):
            out = out[:s] + out[e:]
        out = clean_leak_tokens(out)
        return (out if out.strip() else None), None

    return clean_leak_tokens(text), None
