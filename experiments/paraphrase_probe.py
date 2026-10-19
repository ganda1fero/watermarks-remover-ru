"""Measure the whole-text paraphrase step: speed and what it breaks (numbers, names, language).

usage: python paraphrase_probe.py MODEL [CHUNK_WORDS] [NUM_CTX]
The text is cut into paragraph groups of at most CHUNK_WORDS words (a long text does not fit one
request); the model stays loaded between chunks, so seconds measure generation only.
"""
import json
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "service", "scripts"))
import rewrite_text as r  # noqa: E402

MODEL = sys.argv[1]
CHUNK_WORDS = int(sys.argv[2]) if len(sys.argv) > 2 else 450
os.environ["WATERMARKS_REWRITE_NUM_CTX"] = sys.argv[3] if len(sys.argv) > 3 else "4096"
os.environ["WATERMARKS_REWRITE_KEEP_ALIVE"] = "10m"

NUM = re.compile(r"\d[\d.,]*\d|\d")
CAP = re.compile(r"(?<![.!?]\s)(?<!^)\b[А-ЯЁA-Z][а-яёa-z\-]{2,}")


def chunks(text: str, limit: int) -> list[str]:
    out, cur, n = [], [], 0
    for para in text.split("\n\n"):
        w = len(para.split())
        if cur and n + w > limit:
            out.append("\n\n".join(cur))
            cur, n = [], 0
        cur.append(para)
        n += w
    if cur:
        out.append("\n\n".join(cur))
    return out


def cyr(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    return sum("Ѐ" <= c <= "ӿ" for c in letters) / max(1, len(letters))


rows, outputs = [], {}
for name in ("test_text.txt", "test_text_web.txt"):
    text = open(os.path.join(HERE, name), encoding="utf-8").read().strip()
    pieces, results = chunks(text, CHUNK_WORDS), []
    for piece in pieces:
        t0 = time.time()
        out, st = r.apply_strategy(piece, [("paraphrase", 0.8)], backend="ollama", model=MODEL,
                                   base_url="http://127.0.0.1:11434", api_key=None, timeout=900)
        sec = time.time() - t0
        n_in, n_out = set(NUM.findall(piece)), set(NUM.findall(out))
        c_in, c_out = set(CAP.findall(piece)), set(CAP.findall(out))
        results.append({
            "words": len(piece.split()), "sec": round(sec, 1), "out_words": len(out.split()),
            "numbers_lost": sorted(n_in - n_out), "numbers_new": sorted(n_out - n_in),
            "names_lost": sorted(c_in - c_out), "cyrillic": round(cyr(out), 2),
            "divergence": round(r._lexical_divergence(piece, out), 2),
        })
        outputs.setdefault(name, []).append(out)
        print(name, results[-1], flush=True)
    tot_w, tot_s = sum(x["words"] for x in results), sum(x["sec"] for x in results)
    rows.append((name, tot_w, tot_s, tot_s / tot_w * 100))
    print(f"== {name}: {len(pieces)} chunks, {tot_w} words, {tot_s:.0f} s, {tot_s / tot_w * 100:.1f} s per 100 words",
          flush=True)
json.dump(outputs, open(os.path.join(HERE, "paraphrase_probe_out.json"), "w", encoding="utf-8"), ensure_ascii=False)
try:
    import urllib.request
    urllib.request.urlopen(urllib.request.Request(
        "http://127.0.0.1:11434/api/generate", data=json.dumps({"model": MODEL, "keep_alive": 0}).encode(),
        headers={"Content-Type": "application/json"}), timeout=30).read()
except Exception:
    pass
