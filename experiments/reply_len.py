"""How many tokens do models really spend on a swap reply? (no length cap, 60 s per call)"""
import json
import os
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "service", "scripts"))
import rewrite_text as r  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
text = open(os.path.join(HERE, "test_text.txt"), encoding="utf-8").read().strip()
tokens = r._MLM_TOKEN_RE.split(text)
word_pos = [i for i, t in enumerate(tokens) if any(c.isalnum() for c in t)]
pos_of = {i: p for p, i in enumerate(word_pos)}
cand = [i for i in word_pos if r._swap_candidate(tokens[i])]
picks = r._spread(cand, 60)
prompts = []
for i in picks:
    p, n = pos_of[i], 10
    lo, hi = word_pos[max(0, p - n)], word_pos[min(len(word_pos) - 1, p + n)]
    window = ("".join(tokens[lo:i]) + f"[[{tokens[i]}]]" + "".join(tokens[i + 1:hi + 1])).strip()
    prompts.append(r._swap_prompt("ru", window, tokens[i]))

MODELS = sys.argv[1:] or ["qwen2.5:7b", "yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest", "gemma3:4b", "saiga-mistral-fixed"]
for model in MODELS:
    counts, replies, timeouts = [], [], 0
    for pr in prompts:
        body = json.dumps({"model": model, "stream": False, "keep_alive": "2m",
                           "messages": [{"role": "user", "content": pr}],
                           "options": {"temperature": 0.3, "num_ctx": 2048}}).encode()
        try:
            d = json.loads(urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:11434/api/chat", data=body, headers={"Content-Type": "application/json"}),
                timeout=60).read())
            counts.append(d.get("eval_count", 0))
            replies.append(d["message"]["content"])
        except Exception as e:
            timeouts += 1
    # unload
    try:
        urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:11434/api/generate",
                               data=json.dumps({"model": model, "keep_alive": 0}).encode(),
                               headers={"Content-Type": "application/json"}), timeout=30).read()
    except Exception:
        pass
    counts_sorted = sorted(counts)
    over = [(c, x) for c, x in zip(counts, replies) if c > 24]
    p = lambda q: counts_sorted[min(len(counts_sorted) - 1, int(q * len(counts_sorted)))] if counts_sorted else None
    print(f"{model}: calls ok={len(counts)} timeouts(60s)={timeouts} median={p(0.5)} p90={p(0.9)} max={max(counts) if counts else None} over24={len(over)}")
    for c, x in over[:3]:
        print(f"    {c} tok: {' '.join(x.split())[:140]!r}")
