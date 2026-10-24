#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GenAI 代理诊断与基准工具（学习 jollyxenon/shanghaitech-genai2api 的 bench-speed/doctor）。

用法:
  python bench.py --doctor          # 诊断: 代理存活/凭据/模型清单/一次真实对话
  python bench.py                   # 全部模型速度基准 (TTFT / 解码速度)
  python bench.py --model gpt-6-astra --rounds 3
"""
import argparse
import json
import statistics
import time

import httpx

BASE = "http://127.0.0.1:8000"
KEY = "sk-shanghaitech-genai"
H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def doctor():
    print("=" * 64)
    print("GenAI 代理诊断 (doctor)")
    print("=" * 64)

    # 1. 存活
    try:
        r = httpx.get(f"{BASE}/healthz", timeout=5)
        print(f"[1] 代理存活 /healthz: {r.status_code} {'✓' if r.status_code == 200 else '✗'}")
    except Exception as e:
        print(f"[1] 代理存活: ✗ 无法连接 ({type(e).__name__}) — 服务没启动？双击 start.bat")
        return

    # 2. 上游凭据
    try:
        d = httpx.get(f"{BASE}/health", timeout=15).json()
        auth = d.get("upstream_auth", d)
        print(f"[2] 上游凭据 /health: {json.dumps(auth, ensure_ascii=False)[:120]}")
    except Exception as e:
        print(f"[2] 上游凭据: 查询失败 {type(e).__name__}")

    # 3. 模型清单
    try:
        r = httpx.get(f"{BASE}/v1/models", headers=H, timeout=15)
        ids = [m["id"] for m in r.json()["data"]]
        print(f"[3] 模型清单: {len(ids)} 个 -> {', '.join(ids)}")
    except Exception as e:
        print(f"[3] 模型清单: ✗ {type(e).__name__} (鉴权错或代理异常)")
        return

    # 4. 一次真实对话
    t0 = time.time()
    try:
        r = httpx.post(f"{BASE}/v1/chat/completions", headers=H, json={
            "model": ids[0] if ids else "gpt-6-astra",
            "messages": [{"role": "user", "content": "reply: OK"}], "stream": False,
        }, timeout=120)
        dt = time.time() - t0
        if r.status_code == 200:
            content = r.json()["choices"][0]["message"].get("content") or ""
            print(f"[4] 真实对话: ✓ {dt:.1f}s 回复={content[:20]!r}")
        else:
            print(f"[4] 真实对话: ✗ HTTP {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"[4] 真实对话: ✗ {type(e).__name__} {str(e)[:80]}")

    print("=" * 64)


def bench_model(model: str, rounds: int) -> dict:
    """测 TTFT(首字) 与解码速度：流式请求，统计首 chunk 与整体。"""
    ttfts, decodes, ok = [], [], 0
    for _ in range(rounds):
        t0 = time.time()
        first = None
        chars = 0
        try:
            with httpx.stream("POST", f"{BASE}/v1/chat/completions", headers=H, json={
                "model": model, "stream": True,
                "messages": [{"role": "user", "content": "写一段120字左右的短文，介绍上海。"}],
            }, timeout=180) as r:
                if r.status_code != 200:
                    continue
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    d = line[5:].strip()
                    if d == "[DONE]":
                        break
                    try:
                        p = json.loads(d)
                        ch = (p.get("choices") or [{}])[0]
                        piece = (ch.get("delta", {}) or {}).get("content") or ""
                        if piece:
                            if first is None:
                                first = time.time() - t0
                            chars += len(piece)
                    except Exception:
                        pass
            total = time.time() - t0
            if first is not None and chars > 30:
                ttfts.append(first)
                decodes.append(chars / max(total - first, 1e-6))
                ok += 1
        except Exception:
            continue
    return {
        "model": model, "rounds": rounds, "ok": ok,
        "ttft_med": statistics.median(ttfts) if ttfts else None,
        "decode_med": statistics.median(decodes) if decodes else None,
    }


def main():
    ap = argparse.ArgumentParser(description="GenAI 代理诊断与基准")
    ap.add_argument("--doctor", action="store_true", help="运行诊断")
    ap.add_argument("--model", default=None, help="只测指定模型")
    ap.add_argument("--rounds", type=int, default=2, help="每模型重复轮数")
    args = ap.parse_args()

    if args.doctor:
        doctor()
        return

    models = [args.model] if args.model else [
        m["id"] for m in httpx.get(f"{BASE}/v1/models", headers=H, timeout=15).json()["data"]
    ]
    print(f"{'模型':<24} {'成功':>4} {'TTFT中位':>10} {'解码字/s':>10}")
    print("-" * 56)
    for m in models:
        try:
            r = bench_model(m, args.rounds)
        except Exception as e:
            print(f"{m:<24} ERR {type(e).__name__}")
            continue
        ttft = f"{r['ttft_med']:.2f}s" if r["ttft_med"] else "-"
        dec = f"{r['decode_med']:.0f}" if r["decode_med"] else "-"
        print(f"{r['model']:<24} {r['ok']:>3}/{r['rounds']:<3} {ttft:>10} {dec:>10}")


if __name__ == "__main__":
    main()
