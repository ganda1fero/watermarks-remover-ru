"""Grid for the per-word window swap.  usage: python bench_swap.py MODEL CONFIG[,CONFIG...] [TEXT[,TEXT]]

CONFIG names: ru_n5 ru_n10 ru_n15 ru_n25 ru_s10 (short ru prompt) en_n10 ...
Input is always the ORIGINAL text, so differences come from the swap step only.
Resumable: finished (model, text, config) triples in swap_runs.jsonl are skipped.
"""
import difflib
import json
import os
import random
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "swap_samples")
OUT = os.path.join(HERE, "swap_out")
RUNS = os.path.join(HERE, "swap_runs.jsonl")
for d in (SAMPLES, OUT):
    os.makedirs(d, exist_ok=True)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "service", "scripts"))
import rewrite_text as r  # noqa: E402

TEXTS = {"space": "test_text.txt", "web": "test_text_web.txt"}
CONFIGS = {  # name -> (prompt kind, window words each side)
    "ru_n3": ("ru", 3), "ru_n5": ("ru", 5), "ru_n10": ("ru", 10), "ru_n15": ("ru", 15),
    "ru_n20": ("ru", 20), "ru_n25": ("ru", 25), "ru_n30": ("ru", 30),
    "ru_s10": ("ru_short", 10), "en_n10": ("en", 10), "rujson_n10": ("ru_json", 10), "ruhl_n10": ("ru_hl", 10),
}
VER = os.environ.get("BENCH_VER", "v1")  # v1 = no dictionary/date rules, v2 = with them


def done_keys():
    if not os.path.exists(RUNS):
        return set()
    return {(j.get("ver", "v1"), j["model"], j["text"], j["config"])
            for j in map(json.loads, open(RUNS, encoding="utf-8"))}


def edits(orig, out, ctx=5):
    a, b = r._MLM_TOKEN_RE.split(orig), r._MLM_TOKEN_RE.split(out)
    res = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        res.append([i1, i2, j1, j2])
    # merge edits separated only by whitespace (a changed phrase, e.g. "самому проекту")
    merged = []
    for e in res:
        if merged and all(not t.strip() for t in a[merged[-1][1]:e[0]]):
            merged[-1][1], merged[-1][3] = e[1], e[3]
            # whitespace between them is identical in both versions
        else:
            merged.append(list(e))
    out_edits = []
    for i1, i2, j1, j2 in merged:
        left = "".join(a[max(0, i1 - 2 * ctx):i1]).strip()
        right = "".join(a[i2:i2 + 2 * ctx]).strip()
        out_edits.append(("".join(a[i1:i2]), "".join(b[j1:j2]), left, right))
    return out_edits


def main():
    model, cfgs = sys.argv[1], sys.argv[2].split(",")
    texts = sys.argv[3].split(",") if len(sys.argv) > 3 else list(TEXTS)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", model.replace(":", "").replace(".", "_"))
    os.environ.update({"WATERMARKS_REWRITE_KEEP_ALIVE": "0", "WATERMARKS_REWRITE_NUM_CTX": "4096",
                       "WATERMARKS_SWAP_CTX_TOKENS": "1200"})
    for tname in texts:
        text = open(os.path.join(HERE, TEXTS[tname]), encoding="utf-8").read().strip()
        for cname in cfgs:
            if (VER, model, tname, cname) in done_keys():
                print("skip", model, tname, cname, flush=True)
                continue
            kind, n = CONFIGS[cname]
            os.environ["WATERMARKS_SWAP_PROMPT"], os.environ["WATERMARKS_SWAP_WINDOW"] = kind, str(n)
            t0 = time.time()
            print(time.strftime("%H:%M:%S"), "RUN", model, tname, cname, flush=True)
            try:
                out, st = r.apply_strategy(text, [("swap", 0.2)], backend="ollama", model=model,
                                           base_url="http://127.0.0.1:11434", api_key=None, timeout=900)
            except Exception as e:  # keep the grid going, record the failure
                print("FAILED", repr(e), flush=True)
                with open(RUNS, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ver": VER, "model": model, "text": tname, "config": cname,
                                        "error": repr(e)}) + "\n")
                continue
            sec = round(time.time() - t0, 1)
            sw = st["steps"][0]["swap"]
            ed = edits(text, out)
            same2 = sum(1 for o, n_, *_ in ed if o and n_ and o[-2:].lower() == n_[-2:].lower())
            rec = {"ver": VER, "model": model, "text": tname, "config": cname, "sec": sec, **sw,
                   "edits": len(ed), "suffix2_match": round(same2 / max(1, len(ed)), 2),
                   "fill": round(sw["accepted"] / max(1, sw["target"]), 2)}
            tag = "" if VER == "v1" else f"{VER}_"
            open(os.path.join(OUT, f"{tag}{slug}_{tname}_{cname}.txt"), "w", encoding="utf-8").write(out)
            rnd = random.Random(7)
            pick = rnd.sample(ed, min(30, len(ed)))
            os.makedirs(os.path.join(HERE, "swap_pairs"), exist_ok=True)
            json.dump(ed, open(os.path.join(HERE, "swap_pairs", f"{tag}{slug}_{tname}_{cname}.json"), "w",
                               encoding="utf-8"), ensure_ascii=False)  # every replacement, for labelling
            with open(os.path.join(SAMPLES, f"{tag}{slug}_{tname}_{cname}.txt"), "w", encoding="utf-8") as f:
                for k, (o, n_, left, right) in enumerate(pick, 1):
                    f.write(f"{k}. …{left} [[{o} -> {n_}]] {right}…\n")
            with open(RUNS, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print("  done", sec, "s", json.dumps(rec, ensure_ascii=False), flush=True)
    print("ALL DONE", flush=True)


main()
