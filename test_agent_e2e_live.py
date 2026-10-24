#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 实战端到端测试套件：针对反代在 Agent 框架中的真实使用场景做详尽验证。"""
import httpx
import json
import time

BASE = "http://127.0.0.1:8000/v1"
KEY = "Bearer sk-shanghaitech-genai"
H = {"Authorization": KEY, "Content-Type": "application/json"}
PRIMARY = "gpt-6-astra"

results = []

def report(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  | {detail}" if detail else ""))

def chat(payload, stream=False, timeout=120):
    if stream:
        full, tool_calls, fin, leaked = "", None, None, False
        with httpx.stream("POST", f"{BASE}/chat/completions", headers=H, json=payload, timeout=timeout) as r:
            status = r.status_code
            if status != 200:
                return status, None, None, "", None
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                d = line[5:].strip()
                if d == "[DONE]":
                    break
                try:
                    p = json.loads(d)
                except Exception:
                    continue
                ch_list = p.get("choices") or [{}]
                ch = ch_list[0] if ch_list else {}
                delta = ch.get("delta", {}) or {}
                full += delta.get("content") or ""
                if delta.get("tool_calls"):
                    tool_calls = delta["tool_calls"]
                if ch.get("finish_reason"):
                    fin = ch["finish_reason"]
        return status, full, tool_calls, fin, None
    else:
        r = httpx.post(f"{BASE}/chat/completions", headers=H, json=payload, timeout=timeout)
        if r.status_code != 200:
            return r.status_code, None, None, None, r.text[:200]
        try:
            d = r.json()
        except Exception:
            # 打印非 JSON 响应体，定位问题（不崩套件，记为失败）
            print(f"  [DEBUG] 非 JSON 响应 status={r.status_code} body[:300]={r.text[:300]!r}")
            return r.status_code, None, None, None, f"NON-JSON: {r.text[:200]}"
        ch = d["choices"][0]
        return r.status_code, ch["message"].get("content"), ch["message"].get("tool_calls"), ch.get("finish_reason"), d

TOOLS_2 = [
    {"type": "function", "function": {"name": "web_search", "description": "Search the web",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a local file",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
]
TOOLS_53 = TOOLS_2 + [
    {"type": "function", "function": {"name": f"Tool_{i:02d}", "description": f"Generic tool number {i}",
     "parameters": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}}}
    for i in range(51)
]
SECRET_TOOL_RESULT = "The secret access code is PINEAPPLE-42."

print("=" * 72)
print("Agent 实战端到端测试套件")
print("=" * 72)

# ---- T1 基础对话（非流式基线）----
st, content, tc, fin, _ = chat({"model": PRIMARY, "messages": [{"role": "user", "content": "say OK"}]})
report("T1 基础非流式对话", st == 200 and fin == "stop" and content, f"fin={fin} reply={repr((content or '')[:20])}")

# ---- T2 工具调用（非流式，标准断言）----
st, content, tc, fin, _ = chat({
    "model": PRIMARY,
    "messages": [{"role": "user", "content": "Search the web for AR0234 datasheet"}],
    "tools": TOOLS_2,
})
ok = st == 200 and fin == "tool_calls" and tc and tc[0]["function"]["name"] == "web_search"
try:
    args = json.loads(tc[0]["function"]["arguments"])
    ok = ok and "ar0234" in json.dumps(args).lower()
except Exception:
    ok = False
report("T2 工具调用-非流式", ok, f"fin={fin} name={tc[0]['function']['name'] if tc else None} args={tc[0]['function']['arguments'][:60] if tc else None}")

# ---- T3 工具调用（流式）----
st, content, tc, fin, _ = chat({
    "model": PRIMARY, "stream": True,
    "messages": [{"role": "user", "content": "Search the web for AR0234 datasheet"}],
    "tools": TOOLS_2,
}, stream=True)
ok = st == 200 and fin == "tool_calls" and tc and "index" in tc[0] and tc[0]["function"]["name"] == "web_search"
ok = ok and not content  # 工具调用流不应泄漏 JSON 文本
report("T3 工具调用-流式(带index零泄漏)", ok, f"fin={fin} leak_text={repr((content or '')[:40])}")

# ---- T4 多轮工具循环（第二轮把工具结果喂回，模型必须引用结果）----
round1_st, _, tc, _, _ = chat({
    "model": PRIMARY,
    "messages": [{"role": "user", "content": "Search the web for the secret access code"}],
    "tools": TOOLS_2,
})
round2_messages = [
    {"role": "user", "content": "Search the web for the secret access code"},
    {"role": "assistant", "content": None, "tool_calls": tc},
    {"role": "tool", "tool_call_id": tc[0]["id"] if tc else "call_x",
     "content": SECRET_TOOL_RESULT},
]
st, content, tc2, fin, _ = chat({
    "model": PRIMARY,
    "messages": round2_messages,
    "tools": TOOLS_2,
})
ok = st == 200 and "PINEAPPLE-42" in (content or "")
report("T4 多轮工具循环(工具结果回传后引用)", ok, f"fin={fin} answer含密码={'PINEAPPLE-42' in (content or '')} reply={repr((content or '')[:50])}")

# ---- T5 多工具并行调用 ----
st, content, tc, fin, _ = chat({
    "model": PRIMARY,
    "messages": [{"role": "user", "content": "I need BOTH: the weather in Beijing (web_search) and the content of /tmp/notes.txt (read_file). Call every tool you need now."}],
    "tools": TOOLS_2,
})
n_calls = len(tc) if tc else 0
names = [t["function"]["name"] for t in tc] if tc else []
ok = st == 200 and fin == "tool_calls" and n_calls >= 1
report("T5 多工具并行调用", ok, f"调用数={n_calls} names={names}")

# ---- T6 53 工具规模（ZCode 级别）----
st, content, tc, fin, _ = chat({
    "model": PRIMARY,
    "messages": [{"role": "user", "content": "Search the web for AR0234 datasheet"}],
    "tools": TOOLS_53,
})
ok = st == 200 and fin == "tool_calls" and tc
report("T6 53工具规模", ok, f"fin={fin} name={tc[0]['function']['name'] if tc else None} args={(tc[0]['function']['arguments'][:50] if tc else None)}")

# ---- T7 tool_choice 变体 ----
st, content, tc, fin, _ = chat({
    "model": PRIMARY,
    "messages": [{"role": "user", "content": "hello, just say hi"}],
    "tools": TOOLS_2,
    "tool_choice": "none",
})
ok = st == 200 and fin == "stop" and not tc
report("T7a tool_choice=none 不调用", ok, f"fin={fin} tool_calls={bool(tc)}")

st, content, tc, fin, _ = chat({
    "model": PRIMARY,
    "messages": [{"role": "user", "content": "Please read the file /tmp/notes.txt for me"}],
    "tools": TOOLS_2,
    "tool_choice": {"type": "function", "function": {"name": "read_file"}},
})
forced_ok = tc and tc[0]["function"]["name"] == "read_file"
report("T7b tool_choice=指定函数", st == 200 and fin == "tool_calls" and forced_ok,
       f"fin={fin} name={tc[0]['function']['name'] if tc else None}")

# ---- T8 误报防护（无 tools 时模型讲解 tool_calls JSON 不得被误转为调用）----
st, content, tc, fin, _ = chat({
    "model": PRIMARY,
    "messages": [{"role": "user", "content": "Show me an example JSON body of an OpenAI tool_calls request. Just the JSON in a code block, do not call anything."}],
})
ok = st == 200 and fin == "stop" and not tc and content and "tool_calls" in content
report("T8 无tools时JSON示例不误转为调用", ok, f"fin={fin} tool_calls={bool(tc)} content含tool_calls={'tool_calls' in (content or '')}")

# ---- T9 异常输入防护 ----
r = httpx.post(f"{BASE}/chat/completions", headers=H, json={"model": PRIMARY, "messages": []}, timeout=30)
ok = r.status_code == 400 and r.json()["error"]["param"] == "messages"
report("T9a 空messages→400", ok, f"got={r.status_code}")

r = httpx.get(f"{BASE}/models", headers={"Authorization": "Bearer wrong-key"}, timeout=15)
ok = r.status_code == 401
report("T9b 错误APIKey→401", ok, f"got={r.status_code}")

# ---- T10 Kimi-k3 思考抑制（Agent 场景低延迟）----
t0 = time.time()
st, content, tc, fin, _ = chat({
    "model": "Kimi-k3", "stream": True,
    "messages": [{"role": "user", "content": "1+1=? answer number only"}],
}, stream=True)
dt = time.time() - t0
ok = st == 200 and fin == "stop" and dt < 45
report("T10 Kimi-k3 极速直出(思考抑制)", ok, f"{dt:.1f}s reply={repr((content or '')[:15])}")

# ---- T11 system 消息兼容（客户端常带 system prompt）----
st, content, tc, fin, _ = chat({
    "model": PRIMARY,
    "messages": [
        {"role": "system", "content": "You are a helpful assistant. Always answer in Chinese."},
        {"role": "user", "content": "say OK"},
    ],
})
ok = st == 200 and fin == "stop" and content
report("T11 system消息兼容", ok, f"fin={fin} reply={repr((content or '')[:20])}")

# ---- T12 多轮 + 流式（第二轮工具结果 + stream 同时开，Agent 常用组合）----
st, content, tc, fin, _ = chat({
    "model": PRIMARY, "stream": True,
    "messages": round2_messages,
    "tools": TOOLS_2,
}, stream=True)
ok = st == 200 and "PINEAPPLE-42" in (content or "")
report("T12 多轮工具循环+流式", ok, f"fin={fin} answer含密码={'PINEAPPLE-42' in (content or '')}")

# ---- 汇总 ----
print()
print("=" * 72)
npass = sum(1 for _, ok, _ in results if ok)
print(f"汇总: {npass}/{len(results)} 通过")
for name, ok, detail in results:
    if not ok:
        print(f"  失败: {name} | {detail}")
