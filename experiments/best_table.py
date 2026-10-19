"""Summary table of the best variants (from swap_runs.jsonl + hand labels in swap_grades.json)."""
import json
import os
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")
runs = [json.loads(l) for l in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "swap_runs.jsonl"), encoding="utf-8")]
grades = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "swap_grades.json"), encoding="utf-8"))
Y = "yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest"


def key(r):
    ver = r.get("ver", "v1")
    return f"{ver}|{r['model']}|{r['text']}|{r['config']}" if ver != "v1" else f"{r['model']}|{r['text']}|{r['config']}"


groups = defaultdict(list)
for r in runs:
    if "error" not in r:
        groups[(r.get("ver", "v1"), r["model"], r["config"])].append(r)


def summarize(parts):
    rs = [r for ver, model, cfg in parts for r in groups.get((ver, model, cfg), [])]
    G = n = 0
    for r in rs:
        g = grades.get(key(r))
        if g:
            G += g["G"]
            n += sum(g.values())
    acc, tgt = sum(r["accepted"] for r in rs), sum(r["target"] for r in rs)
    calls = sum(r["calls"] + r.get("verify_calls", 0) for r in rs)
    k = len(rs)
    return dict(
        good=100 * G / n if n else float("nan"), n=n, sec=sum(r["sec"] for r in rs) / k, quota=100 * acc / tgt,
        refused=sum(r["refused"] for r in rs) / k,
        rej=sum(r["invalid"] + r.get("dict_rejected", 0) + r.get("form_rejected", 0) for r in rs) / k,
        vrej=sum(r.get("verify_rejected", 0) for r in rs) / k, calls_per=calls / acc, runs=k,
    )


ROWS = [  # in my priority order
    ("YandexGPT v3, n=10, проверка смысла 12 (итог)", [("v3a", Y, "ru_n10"), ("v3final", Y, "ru_n10")]),
    ("YandexGPT v3, n=5, проверка смысла 12", [("v3a", Y, "ru_n5")]),
    ("YandexGPT v3, n=10, строгая проверка 17", [("v3a_hi", Y, "ru_n10")]),
    ("YandexGPT v2, n=10 (без проверки смысла)", [("v2", Y, "ru_n10")]),
    ("YandexGPT v2, n=5 (без проверки смысла)", [("v2", Y, "ru_n5")]),
    ("gemma3:4b v2, n=10", [("v2", "gemma3:4b", "ru_n10")]),
    ("qwen2.5:14b v1, RU-короткий, n=10", [("v1", "qwen2.5:14b", "ru_s10")]),
    ("qwen2.5:7b v2, n=10", [("v2", "qwen2.5:7b", "ru_n10")]),
    ("Saiga-Mistral (исправл.) v2, n=10", [("v2", "saiga-mistral-fixed", "ru_n10")]),
]
print("| # | Вариант | годных | оценено | сек/прогон | норма | отказов NULL | отбраковано кодом | отсеяно проверкой смысла | запросов на замену |")
print("|---|---|---|---|---|---|---|---|---|---|")
for i, (name, parts) in enumerate(ROWS, 1):
    a = summarize(parts)
    print(f"| {i} | {name} | {a['good']:.0f}% | {a['n']} | {a['sec']:.0f} | {a['quota']:.0f}% | {a['refused']:.0f} | "
          f"{a['rej']:.0f} | {a['vrej']:.0f} | {a['calls_per']:.1f} |")
