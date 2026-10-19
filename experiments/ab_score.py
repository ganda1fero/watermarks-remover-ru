"""Score EVERY replacement of several runs with the meaning checker (the one used by v3a).

usage: python ab_score.py ARM=prefix [ARM=prefix ...]
  prefix = swap_pairs file prefix without "_<text>_<config>.json", e.g.
  v2_yandex_YandexGPT-5-Lite-8B-instruct-GGUFlatest  (+ _space_ru_n10.json / _web_ru_n10.json)
"""
import glob
import json
import math
import os
import statistics as st
import sys

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "service", "scripts"))
import rewrite_text as r  # noqa: E402

MODEL = "yandex/YandexGPT-5-Lite-8B-instruct-GGUF:latest"
BASE = "http://127.0.0.1:11434"
THR = 12.0
os.environ["WATERMARKS_REWRITE_NUM_CTX"] = "2048"


def score_file(path):
    out = []
    for orig, new, left, right in json.load(open(path, encoding="utf-8")):
        if not orig.strip() or not new.strip():
            continue
        out.append(r._verify_score(BASE, MODEL, r._verify_prompt(left, orig, new, right), 60, "5m"))
    return out


def main():
    cache_p = os.path.join(HERE, "ab_scores.json")
    cache = json.load(open(cache_p, encoding="utf-8")) if os.path.exists(cache_p) else {}
    for spec in sys.argv[1:]:
        arm, rest = spec.split("=", 1)
        prefix, cfg = rest.split("@", 1)
        allv = []
        per = {}
        for text in ("space", "web"):
            files = glob.glob(os.path.join(HERE, "swap_pairs", f"{prefix}_{text}_{cfg}.json"))
            for f in files:
                key = os.path.basename(f)
                if key not in cache:
                    cache[key] = score_file(f)
                    json.dump(cache, open(cache_p, "w", encoding="utf-8"))
                per[text] = cache[key]
                allv += cache[key]
        if not allv:
            print(f"{arm}: no data for {prefix}")
            continue
        def share(v): return sum(x >= THR for x in v) / len(v)
        p = share(allv)
        se = math.sqrt(p * (1 - p) / len(allv))
        print(f"{arm:14s} n={len(allv):3d}  mean={st.mean(allv):6.2f}  median={st.median(allv):6.2f}  "
              f"accepted(>={THR:g})={p:4.0%} ±{se:.0%}   space={share(per['space']):4.0%}({len(per['space'])})  web={share(per['web']):4.0%}({len(per['web'])})")
    try:
        import urllib.request
        urllib.request.urlopen(urllib.request.Request(BASE + "/api/generate",
            data=json.dumps({"model": MODEL, "keep_alive": 0}).encode(),
            headers={"Content-Type": "application/json"}), timeout=30).read()
    except Exception:
        pass


main()
