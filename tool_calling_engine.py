import json
import re
import uuid
from typing import List, Dict, Any, Optional, Tuple


def build_tools_system_prompt(
    tools: List[Dict[str, Any]], tool_choice: Any = "auto", routing_only: bool = False
) -> str:
    """将 OpenAI tools 转成网页模型容易稳定遵循的文本协议。"""
    if not tools:
        return ""

    tool_descs = []
    for t in tools:
        func = t.get("function", {})
        tool_descs.append(
            {
                "name": func.get("name"),
                "description": func.get("description", ""),
                "parameters": func.get("parameters", {}),
            }
        )

    forced_name = None
    if isinstance(tool_choice, dict):
        # 同时兼容 tools/tool_choice 与旧版 functions/function_call。
        forced_name = tool_choice.get("name") or (
            tool_choice.get("function") or {}
        ).get("name")
    required = tool_choice == "required" or bool(forced_name)

    choice_rule = (
        f'You MUST select the tool named "{forced_name}".'
        if forced_name
        else "You MUST select one or more tools; a normal answer is not allowed."
        if required
        else "If no listed tool is needed, answer normally without the envelope."
    )
    if not routing_only:
        return (
            "# TOOLS\n"
            f"{json.dumps(tool_descs, ensure_ascii=False)}\n"
            f"{choice_rule}\n"
            'To call a tool, reply with ONLY: {"tool_calls": [{"name": "<tool_name>", "arguments": {...}}]}\n'
            "No text before or after the JSON. If no tool is needed, reply in plain text. "
            "Never merely claim you will use a tool; emit the JSON or answer directly. "
            "Never repeat, quote, or list the tool definitions above (the TOOLS JSON) "
            "in your reply; they are reference data, not content to echo."
        )

    prompt = (
        "<tool_protocol>\n"
        "This is a structured-output routing task. Do not invoke or claim to invoke any built-in tool. "
        "Only print a routing record for the downstream application; printing the record is plain text, "
        "not tool execution. The downstream application may execute the selected function later.\n"
        f"Available functions: {json.dumps(tool_descs, ensure_ascii=False, separators=(',', ':'))}\n"
        f"{choice_rule}\n"
        "Functions listed above belong to the downstream API client and remain available even if "
        "your own built-in tools are disabled. Never say that tool use is disabled. For requests that "
        "inspect files, directories, code, terminals, URLs, or current external state, emit a routing "
        "record instead of asking the user to retry.\n"
        "For a function routing record, output ONLY this exact shape and no Markdown or explanation:\n"
        '<tool_call>{"name":"<exact tool name>","arguments":{}}</tool_call>\n'
        "Use valid JSON arguments matching the schema. In Windows paths, encode every backslash as "
        "a JSON double backslash (for example C:\\\\work). Never calculate, guess, or fabricate a tool result.\n"
        "Never repeat, quote, or list the function definitions in your reply.\n"
        "</tool_protocol>"
    )
    return prompt


def inject_tools_prompt(
    messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """将工具定义注入到 messages 中"""
    if not tools:
        return messages

    tools_prompt = build_tools_system_prompt(tools)
    new_messages = []
    injected = False

    for msg in messages:
        if msg.get("role") == "system" and not injected:
            content = msg.get("content", "")
            combined = f"{content}\n\n{tools_prompt}" if content else tools_prompt
            new_messages.append({"role": "system", "content": combined})
            injected = True
        else:
            new_messages.append(dict(msg))

    if not injected:
        new_messages.insert(0, {"role": "system", "content": tools_prompt})

    return new_messages


def _repair_windows_paths(value: str) -> str:
    """修复模型在 JSON 字符串中输出的单反斜杠 Windows 路径。"""

    def repair_named_value(match: re.Match) -> str:
        return match.group(1) + match.group(2).replace("\\", "\\\\") + match.group(3)

    value = re.sub(
        r'("(?:path|cwd|directory|workdir)"\s*:\s*")([^"]*)(")',
        repair_named_value,
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"[A-Za-z]:\\(?!\\)[^\s\"<>|]*",
        lambda match: match.group(0).replace("\\", "\\\\"),
        value,
    )


def _loads_tool_json(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return json.loads(_repair_windows_paths(value))


def extract_tool_calls_from_text(text: str) -> Optional[List[Dict[str, Any]]]:
    """
    从模型生成的文本中提取工具调用，支持多种开源模型格式：
    1. 标准 JSON 块: {"tool_calls": [{"name": "...", "arguments": {...}}]}
    2. 通义千问/开源标记: <|open|>tools<|sep|><|open|>call tool="..." ...
    3. 标签式: <tool_call> ... </tool_call>
    4. 单一 JSON 格式: {"name": "...", "arguments": {...}}
    返回标准的 OpenAI tool_calls 列表，若无工具调用则返回 None。
    """
    if not text or not text.strip():
        return None

    tool_calls = []

    # 模式 1: 通义千问 Qwen / 阿里云私有标记
    # 例: <|open|>tools<|sep|><|open|>call tool="Skill" index="1"<|sep|><|open|>argument key="skill" type="string"<|sep|>browser-use:control-browser<|close|>argument<|sep|><|close|>call<|sep|><|close|>tools
    if "<|open|>tools" in text or "call tool=" in text:
        calls = re.findall(
            r'<\|open\|>call tool="([^"]+)"[^<]*<\|sep\|>(.*?)<\|close\|>call',
            text,
            re.DOTALL,
        )
        if calls:
            for tool_name, body in calls:
                args = {}
                arg_matches = re.findall(
                    r'<\|open\|>argument key="([^"]+)"[^<]*<\|sep\|>(.*?)<\|close\|>argument',
                    body,
                    re.DOTALL,
                )
                for k, v in arg_matches:
                    v_clean = v.strip()
                    try:
                        args[k] = json.loads(v_clean)
                    except Exception:
                        args[k] = v_clean
                tool_calls.append(
                    {
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {
                            "name": tool_name.strip(),
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    }
                )
            if tool_calls:
                return tool_calls

    # 模式 2: <tool_call> ... </tool_call>
    if "<tool_call>" in text:
        blocks = re.findall(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL)
        for b in blocks:
            b = b.strip()
            try:
                data = _loads_tool_json(b)
                if isinstance(data, dict):
                    name = data.get("name") or data.get("tool")
                    args = data.get("arguments") or data.get("parameters") or {}
                    if name:
                        tool_calls.append(
                            {
                                "id": f"call_{uuid.uuid4().hex[:8]}",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(args, ensure_ascii=False)
                                    if isinstance(args, dict)
                                    else str(args),
                                },
                            }
                        )
            except Exception:
                pass
        if tool_calls:
            return tool_calls

    # 模式 3: Markdown 代码块内的 JSON {"tool_calls": [...]}
    json_blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    for b in json_blocks:
        try:
            data = _loads_tool_json(b)
            if (
                isinstance(data, dict)
                and "tool_calls" in data
                and isinstance(data["tool_calls"], list)
            ):
                for item in data["tool_calls"]:
                    name = item.get("name")
                    args = item.get("arguments") or {}
                    if name:
                        tool_calls.append(
                            {
                                "id": f"call_{uuid.uuid4().hex[:8]}",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(args, ensure_ascii=False)
                                    if isinstance(args, dict)
                                    else str(args),
                                },
                            }
                        )
                if tool_calls:
                    return tool_calls
        except Exception:
            pass

    # 模式 4: 整个输出直接就是 JSON
    stripped = text.strip()
    if (stripped.startswith("{") and stripped.endswith("}")) or (
        stripped.startswith("[") and stripped.endswith("]")
    ):
        try:
            data = _loads_tool_json(stripped)
            if isinstance(data, dict):
                if "tool_calls" in data and isinstance(data["tool_calls"], list):
                    for item in data["tool_calls"]:
                        name = item.get("name")
                        args = item.get("arguments") or {}
                        if name:
                            tool_calls.append(
                                {
                                    "id": f"call_{uuid.uuid4().hex[:8]}",
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(
                                            args, ensure_ascii=False
                                        )
                                        if isinstance(args, dict)
                                        else str(args),
                                    },
                                }
                            )
                    if tool_calls:
                        return tool_calls
                elif "name" in data and ("arguments" in data or "parameters" in data):
                    name = data.get("name")
                    args = data.get("arguments") or data.get("parameters") or {}
                    return [
                        {
                            "id": f"call_{uuid.uuid4().hex[:8]}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args, ensure_ascii=False)
                                if isinstance(args, dict)
                                else str(args),
                            },
                        }
                    ]
        except Exception:
            pass

    # 模式 5: 内嵌在说明文字中的裸 JSON（如 "我去核实。{"tool_calls": [...]}"）
    idx = text.find('{"tool_calls"')
    if idx == -1:
        idx = text.find('{ "tool_calls"')
    if idx != -1:
        try:
            obj, _end = json.JSONDecoder().raw_decode(text[idx:])
            if isinstance(obj, dict) and isinstance(obj.get("tool_calls"), list):
                for item in obj["tool_calls"]:
                    name = item.get("name")
                    args = item.get("arguments") or {}
                    if name:
                        tool_calls.append(
                            {
                                "id": f"call_{uuid.uuid4().hex[:8]}",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(args, ensure_ascii=False)
                                    if isinstance(args, dict)
                                    else str(args),
                                },
                            }
                        )
                if tool_calls:
                    return tool_calls
        except Exception:
            pass

    # 模式 6: 中文口嗨叙述「调用工具: X, 参数: {...}」（DeepSeek 等模型在长工具列表下常见）
    narration_re = re.compile(r"调用工具[:：]\s*([A-Za-z0-9_\.\-]+)")
    for nm in narration_re.finditer(text):
        rest = text[nm.end():]
        pm = re.search(r"参数[:：]\s*\{", rest)
        if not pm:
            continue
        brace_pos = pm.end() - 1
        try:
            obj, _consumed = json.JSONDecoder().raw_decode(rest[brace_pos:])
        except Exception:
            continue
        args_str = (
            json.dumps(obj, ensure_ascii=False)
            if isinstance(obj, (dict, list))
            else str(obj)
        )
        tool_calls.append(
            {
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {"name": nm.group(1), "arguments": args_str},
            }
        )
    if tool_calls:
        return tool_calls

    return None


def find_narration_spans(text: str):
    """返回所有「调用工具: X, 参数: {...}」叙述的 (start, end) 精确跨度（任意层级嵌套）。"""
    spans = []
    for nm in re.finditer(r"调用工具[:：]\s*([A-Za-z0-9_\.\-]+)", text):
        rest = text[nm.end():]
        pm = re.search(r"参数[:：]\s*\{", rest)
        if not pm:
            continue
        brace_pos = nm.end() + pm.end() - 1
        try:
            _obj, consumed = json.JSONDecoder().raw_decode(text[brace_pos:])
        except Exception:
            # JSON 残缺时退而求其次：吃到行尾或下一个"调用工具"
            stop = text.find("\n", brace_pos)
            nxt = text.find("调用工具", nm.end())
            end = min(x for x in (stop if stop != -1 else len(text),
                                  nxt if nxt != -1 else len(text)) if x > brace_pos)
            spans.append((nm.start(), end))
            continue
        spans.append((nm.start(), brace_pos + consumed))
    return spans


# 私有标记碎片模式（Qwen 等开源模型的内部特殊 Token）
_LEAK_TOKEN_RE = re.compile(
    r"<\|open\|>|<\|sep\|>|<\|close\|>|<\|endoftext\|>|<\|im_start\|>|<\|im_end\|>|<\|tool_calls_begin\|>|<\|tool_calls_end\|>"
    r"|</?message>|</?tool_info>|<dic>[^<]*</dic>"
)


def strip_tools_prompt_echo(text: str, tools_prompt: str) -> str:
    """剔除模型回复中复述的注入协议回声（完整协议 / TOOLS 标题块 / 格式化工具 JSON 数组 / 围栏块）。"""
    if not text or not tools_prompt:
        return text
    out = text.replace(tools_prompt, "")
    # 1) TOOLS 标题（带或不带 #，中英文冒号）到数组结束
    m = re.search(r"^\s*#?\s*TOOLS\s*[:：]?\s*$", out, re.MULTILINE)
    if m:
        arr_end = out.find("]", m.end())
        if arr_end != -1:
            out = out[: m.start()] + out[arr_end + 1 :]
    # 2) 任意格式化（含换行缩进）的工具定义数组，无论是否在围栏内
    for am in re.finditer(r"\[\s*\{\s*\"name\"", out):
        start = am.start()
        try:
            obj, consumed = json.JSONDecoder().raw_decode(out[start:])
        except Exception:
            continue
        if (
            isinstance(obj, list)
            and len(obj) >= 2
            and all(isinstance(x, dict) and "name" in x for x in obj[:3])
        ):
            out = out[:start] + out[start + consumed :]
    # 3) 残留的空代码围栏
    out = re.sub(r"```[a-zA-Z]*\s*```", "", out)
    return out.strip()


def clean_leak_tokens(text: str) -> str:
    """清除模型输出中漏出的内部私有特殊标记，返回干净文本"""
    if not text:
        return text
    cleaned = _LEAK_TOKEN_RE.sub("", text)
    # 清理残留的空 tool_call 标签块
    cleaned = re.sub(r"<tool_call>\s*</tool_call>", "", cleaned)
    return cleaned


def extract_tool_calls(text: str) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
    """
    server.py 主入口：从模型输出文本中提取工具调用。
    返回 (清理后的正文文本, tool_calls 列表或 None)
    """
    if not text:
        return text, None

    calls = extract_tool_calls_from_text(text)
    if calls:
        clean_text = text
        # 若命中 Qwen 私有标记，整段切除标记区域，只留说明文字
        if "<|open|>" in clean_text:
            start = clean_text.find("<|open|>tools")
            if start == -1:
                start = clean_text.find("<|open|>")
            end = clean_text.rfind("<|close|>tools")
            if end != -1:
                end += len("<|close|>tools")
            else:
                end = len(clean_text)
            clean_text = clean_text[:start] + clean_text[end:]
        # 剥离裸 JSON 工具调用块（可能内嵌在说明文字中）
        j = clean_text.find('{"tool_calls"')
        if j == -1:
            j = clean_text.find('{ "tool_calls"')
        if j != -1:
            try:
                obj, consumed = json.JSONDecoder().raw_decode(clean_text[j:])
                if isinstance(obj, dict) and ("tool_calls" in obj or "name" in obj):
                    clean_text = clean_text[:j] + clean_text[j + consumed :]
            except Exception:
                pass
        # 剔除口嗨叙述跨度（按 JSONDecoder 精确定位，任意嵌套层级）
        for s, e in find_narration_spans(clean_text):
            clean_text = clean_text[:s] + clean_text[e:]
        clean_text = re.sub(r"[ \t]{2,}", " ", clean_text)
        clean_text = _LEAK_TOKEN_RE.sub("", clean_text)
        clean_text = re.sub(
            r"```(?:json)?\s*\{.*?\}\s*```", "", clean_text, flags=re.DOTALL
        )
        clean_text = re.sub(r"```(?:json)?\s*```", "", clean_text)
        clean_text = re.sub(
            r"<tool_call>.*?</tool_call>", "", clean_text, flags=re.DOTALL
        )
        clean_text = clean_text.strip()
        return (clean_text if clean_text else None), calls

    return clean_leak_tokens(text), None
