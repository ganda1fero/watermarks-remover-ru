"""Compare second steps after the whole-text paraphrase on the two test texts.

Per chunk (paragraph groups of at most CHUNK_WORDS words):
  A = paraphrase@0.8
  B = A -> mlm@0.2        (fill-mask model WATERMARKS_MLM_MODEL, default xlm-roberta-base)
  C = A -> swap@0.2       (word swap with the meaning check, the current default strategy)

usage: python strategy_probe.py MODEL [CHUNK_WORDS] [NUM_CTX]
Metrics are automatic: numbers lost, name stems lost, sentences lost, share of unknown words
(words not in the dictionary and not in the source), lexical divergence from the original, and a
paragraph-level meaning score from the same checker the swap step uses.
"""
import json
import os
import re
import statistics as st
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
os.environ.setdefault("WATERMARKS_MLM_MODEL", "xlm-roberta-base")
BASE = "http://127.0.0.1:11434"
KW = dict(backend="ollama", model=MODEL, base_url=BASE, api_key=None, timeout=900)

NUM = re.compile(r"\d[\d.,]*\d|\d")
NAME = re.compile(r"(?<=[а-яёa-z,;:]\s)[А-ЯЁA-Z][а-яёa-z\-]{3,}")  # capitalised, not after a full stop
SENT = re.compile(r"[.!?…]+(?:\s|$)")
WORD = re.compile(r"[А-Яа-яЁё]{4,}")
morph = r._get_morph()


def chunks(text, limit):
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


def stems(words):
    return {w.lower()[:5] for w in words}


def metrics(src, out):
    n_src, n_out = set(NUM.findall(src)), set(NUM.findall(out))
    names_src = stems(NAME.findall(src))
    out_stems = {w.lower()[:5] for w in re.findall(r"[А-Яа-яЁёA-Za-z\-]{4,}", out)}
    src_vocab = {w.lower() for w in WORD.findall(src)}
    out_words = [w.lower() for w in WORD.findall(out)]
    unknown = [w for w in out_words if w not in src_vocab and morph and not morph.word_is_known(w)]
    return {
        "numbers_lost": sorted(n_src - n_out),
        "names_lost": sorted(names_src - out_stems),
        "sentences": (len(SENT.findall(src)), len(SENT.findall(out))),
        "unknown_words": len(unknown), "words_checked": len(out_words),
        "divergence": r._lexical_divergence(src, out),
    }


def meaning(src, out):
    """Checker scores for aligned paragraphs (None when the paragraph count changed)."""
    a, b = [p for p in src.split("\n\n") if p.strip()], [p for p in out.split("\n\n") if p.strip()]
    if len(a) != len(b):
        return None
    return [r._verify_score(BASE, MODEL, r._verify_prompt("", x, y, ""), 180, "10m") for x, y in zip(a, b)]


arms = {"A paraphrase": [], "B +mlm": [], "C +swap(check)": []}
secs = {k: 0.0 for k in arms}
words_total = 0
store = {}
for name in ("test_text.txt", "test_text_web.txt"):
    text = open(os.path.join(HERE, name), encoding="utf-8").read().strip()
    for piece in chunks(text, CHUNK_WORDS):
        words_total += len(piece.split())
        t0 = time.time()
        a_out, _ = r.apply_strategy(piece, [("paraphrase", 0.8)], **KW)
        t_a = time.time() - t0
        t0 = time.time()
        b_out, _ = r.apply_strategy(a_out, [("mlm", 0.2)], **KW)
        t_b = time.time() - t0
        os.environ["WATERMARKS_SWAP_VERIFY"] = "1"
        t0 = time.time()
        c_out, _ = r.apply_strategy(a_out, [("swap", 0.2)], **KW)
        t_c = time.time() - t0
        os.environ["WATERMARKS_SWAP_VERIFY"] = "0"
        for arm, out, t in (("A paraphrase", a_out, t_a), ("B +mlm", b_out, t_a + t_b),
                            ("C +swap(check)", c_out, t_a + t_c)):
            m = metrics(piece, out)
            m["meaning"] = meaning(piece, out)
            arms[arm].append(m)
            secs[arm] += t
        store.setdefault(name, []).append({"src": piece, "A": a_out, "B": b_out, "C": c_out})
        print(name, len(piece.split()), "words; sec A/B/C =", round(t_a), round(t_b), round(t_c), flush=True)

json.dump(store, open(os.path.join(HERE, "strategy_probe_out.json"), "w", encoding="utf-8"), ensure_ascii=False)
print()
print("| вариант | сек на 100 слов | числа потеряны | основы имён потеряны | предложений (было→стало) | неизвестных слов | расхождение с оригиналом | смысл по абзацам ≥12 |")
print("|---|---|---|---|---|---|---|---|")
for arm, ms in arms.items():
    nl = sum(len(m["numbers_lost"]) for m in ms)
    names = sum(len(m["names_lost"]) for m in ms)
    s_in, s_out = sum(m["sentences"][0] for m in ms), sum(m["sentences"][1] for m in ms)
    unk = sum(m["unknown_words"] for m in ms)
    chk = sum(m["words_checked"] for m in ms)
    div = st.mean(m["divergence"] for m in ms)
    sc = [x for m in ms if m["meaning"] for x in m["meaning"]]
    acc = f"{100 * sum(x >= 12 for x in sc) / len(sc):.0f}% ({len(sc)} абз.)" if sc else "n/a"
    print(f"| {arm} | {secs[arm] / words_total * 100:.1f} | {nl} | {names} | {s_in}→{s_out} | {unk} из {chk} ({100 * unk / max(1, chk):.1f}%) | {div:.2f} | {acc} |")
try:
    import urllib.request
    urllib.request.urlopen(urllib.request.Request(
        BASE + "/api/generate", data=json.dumps({"model": MODEL, "keep_alive": 0}).encode(),
        headers={"Content-Type": "application/json"}), timeout=30).read()
except Exception:
    pass
