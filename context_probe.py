#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下文上限实测工具（改编自 jollyxenon/shanghaitech-genai2api tools/context_probe.py）。

原理（ZEBRA 标记复述法）：在提示词中埋入 N 次标记词，要求模型逐字复述；
能正确复述的最大的 N 即为该模型可用的有效上下文。二分搜索收敛。

用法:
  python context_probe.py --model DeepSeek-V4.1-Flash
  python context_probe.py --model gpt-6-astra --min 1024 --max 131072
"""
import argparse
import json
import httpx

BASE = "http://127.0.0.1:8000/v1"
KEY = "sk-shanghaitech-genai"
H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
MARKER = "ZEBRA"


def probe(model: str, count: int) -> bool:
    """埋 count 个标记，检查模型是否全部复述。"""
    filler = " ".join([MARKER] * count)
    prompt = (
        f"The following text contains the word {MARKER} repeated {count} times:\n"
        f"{filler}\n"
        f"How many times does the word {MARKER} appear? Reply with only the number."
    )
    try:
        r = httpx.post(f"{BASE}/chat/completions", headers=H, json={
            "model": model, "max_tokens": 2000, "stream": False,
            "messages": [{"role": "user", "content": prompt}],
        }, timeout=300)
        if r.status_code != 200:
            return False
        text = r.json()["choices"][0]["message"].get("content") or ""
        # 模型可能输出 "The word ZEBRA appears N times" 或纯数字
        import re
        nums = re.findall(r"\d+", text)
        return bool(nums) and int(nums[-1]) == count
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="上下文上限实测（ZEBRA 标记复述法）")
    ap.add_argument("--model", required=True)
    ap.add_argument("--min", type=int, default=64, help="搜索下限(标记数)")
    ap.add_argument("--max", type=int, default=65536, help="搜索上限(标记数)")
    ap.add_argument("--rounds", type=int, default=1, help="每档验证次数(去抖)")
    args = ap.parse_args()

    print(f"模型: {args.model} | 搜索区间: [{args.min}, {args.max}]")
    lo, hi = args.min, args.max
    # 上界先行确认：最大档若通过，直接报告 >= max
    if probe(args.model, hi):
        print(f"✓ {hi} 个标记全部复述正确 —— 有效上下文 ≥ {hi}（调大 --max 可继续测）")
        return
    while lo < hi:
        mid = (lo + hi + 1) // 2
        results = [probe(args.model, mid) for _ in range(args.rounds)]
        ok = sum(results) > len(results) / 2
        print(f"  {'✓' if ok else '✗'} {mid:>7} 个标记 ({sum(results)}/{len(results)})")
        if ok:
            lo = mid
        else:
            hi = mid - 1
    # 每个标记约 1 token，结果即有效上下文 token 数的量级估计
    print(f"\n结论: {args.model} 有效上下文约 {lo} tokens（ZEBRA 复述法估测）")


if __name__ == "__main__":
    main()
