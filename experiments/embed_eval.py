"""Проверка смысла эмбеддингами: насколько косинус между исходной и новой фразой отделяет годные замены от плохих.

usage: python embed_eval.py [EMBED_MODEL] [CHECKER_MODEL]
EMBED_MODEL (по умолчанию paraphrase-multilingual) сравнивается с проверкой вторым запросом к CHECKER_MODEL
(та же формула, что в рабочем режиме: log P(ДА) - log P(НЕТ), окно ±5 слов), если CHECKER_MODEL задан.
Выборка: все размеченные вручную замены из swap_labels.json (G = годная, остальное = негодная).
Печатает AUC (1.0 идеально, 0.5 случайно) и долю годных замен, прошедших порог при отсечении 80% негодных.
"""
import json
import math
import os
import re
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
URL = "http://127.0.0.1:11434"
EMBED = sys.argv[1] if len(sys.argv) > 1 else "paraphrase-multilingual"
CHECKER = sys.argv[2] if len(sys.argv) > 2 else None
lab = json.load(open(os.path.join(HERE, "swap_labels.json"), encoding="utf-8"))
LINE = re.compile(r"^(\d+)\. …(.*?)\s*\[\[(.*?) -> (.*?)\]\]\s*(.*?)…?$")


def slug(model):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", model.replace(":", "").replace(".", "_"))


def load_set():
    items = []
    for key, labels in lab.items():
        ver, model, text, cfg = key.split("|")
        path = os.path.join(HERE, "swap_samples", f"{ver}_{slug(model)}_{text}_{cfg}.txt")
        if not os.path.exists(path):
            continue
        rows = re.split(r"\n(?=\d+\. …)", open(path, encoding="utf-8").read().strip())
        for row, y in zip(rows, labels):
            m = LINE.match(" ".join(row.split()))
            if m and m.group(3).strip() and m.group(4).strip():
                _, left, orig, new, right = m.groups()
                items.append({"left": left, "orig": orig, "new": new, "right": right, "y": y})
    return items


def post(path, body, timeout=120):
    req = urllib.request.Request(URL + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def tail(s, n):
    return " ".join(s.split()[-n:])


def head(s, n):
    return " ".join(s.split()[:n])


def cosine(a, b):
    d = sum(x * y for x, y in zip(a, b))
    return d / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) + 1e-9)


def auc(pos, neg):
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / max(1, len(pos) * len(neg))


def embed_scores(items):
    inputs = []
    for it in items:
        l, r = tail(it["left"], 5), head(it["right"], 5)
        inputs += [f"{l} {it['orig']} {r}", f"{l} {it['new']} {r}"]
    vecs = []
    for i in range(0, len(inputs), 64):
        vecs += post("/api/embed", {"model": EMBED, "input": inputs[i:i + 64], "keep_alive": "5m"})["embeddings"]
    return [cosine(vecs[2 * k], vecs[2 * k + 1]) for k in range(len(items))]


PROMPT = (
    "Исходная фраза: {left} {orig} {right}\nНовая фраза: {left} {new} {right}\n\n"
    "Значат ли эти две фразы одно и то же, и грамотна ли новая? Если есть сомнение, ответь НЕТ.\n"
    "Ответ: ДА или НЕТ."
)


def checker_scores(items):
    out = []
    for it in items:
        p = PROMPT.format(left=tail(it["left"], 5), orig=it["orig"], new=it["new"], right=head(it["right"], 5))
        r = post("/api/chat", {
            "model": CHECKER, "stream": False, "keep_alive": "10m", "logprobs": True, "top_logprobs": 10,
            "messages": [{"role": "user", "content": p}],
            "options": {"temperature": 0, "num_predict": 6, "num_ctx": 2048},
        })
        yes = no = 0.0
        for entry in r.get("logprobs") or []:
            if not str(entry.get("token", "")).strip():
                continue
            for t in entry.get("top_logprobs") or []:
                w = str(t.get("token", "")).strip().upper().replace("Ё", "Е").rstrip(".,:!")
                if w in ("ДА", "YES"):
                    yes += math.exp(t["logprob"])
                elif w in ("НЕТ", "НЕ", "NO"):
                    no += math.exp(t["logprob"])
            break
        out.append(math.log(yes + 1e-9) - math.log(no + 1e-9))
    return out


def report(name, scores, items, secs):
    pos = [s for s, i in zip(scores, items) if i["y"] == "G"]
    neg = [s for s, i in zip(scores, items) if i["y"] != "G"]
    thr = sorted(neg)[int(0.8 * len(neg))]  # 80% of bad replacements fall below this
    kept = sum(s > thr for s in pos) / max(1, len(pos))
    print(f"{name:34s} AUC={auc(pos, neg):.3f}  годных при отсечении 80% негодных: {kept:.0%}  "
          f"({len(pos)} годных / {len(neg)} негодных, {secs:.0f} с)", flush=True)


def main():
    items = load_set()
    print("размеченных замен:", len(items), flush=True)
    t0 = time.time()
    report(f"эмбеддинги {EMBED}", embed_scores(items), items, time.time() - t0)
    if CHECKER:
        t0 = time.time()
        sc = checker_scores(items)
        report(f"проверка запросом {CHECKER}", sc, items, time.time() - t0)
        json.dump({"model": CHECKER, "scores": sc, "good": [i["y"] == "G" for i in items]},
                  open(os.path.join(HERE, f"verify_scores_{slug(CHECKER)}.json"), "w", encoding="utf-8"))


main()
