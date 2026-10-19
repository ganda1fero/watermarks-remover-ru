"""Meaning-check validator with confidence scores.  usage: python validator_eval2.py MODEL [PROMPT,...]

score = log P(ДА) - log P(НЕТ) of the first answer token.  Tuned on `space` samples (dev), confirmed on `web` (test).
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
lab = json.load(open(os.path.join(HERE, "swap_labels.json"), encoding="utf-8"))
LINE = re.compile(r"^(\d+)\. …(.*?)\s*\[\[(.*?) -> (.*?)\]\]\s*(.*?)…?$")


def slug(model):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", model.replace(":", "").replace(".", "_"))


def load_set():
    items = []
    for key, labels in lab.items():
        ver, model, text, cfg = key.split("|")
        path = os.path.join(HERE, "swap_samples", f"{ver}_{slug(model)}_{text}_{cfg}.txt")
        rows = re.split(r"\n(?=\d+\. …)", open(path, encoding="utf-8").read().strip())
        for row, y in zip(rows, labels):
            m = LINE.match(" ".join(row.split()))
            if not m:
                continue
            _, left, orig, new, right = m.groups()
            if orig.strip() and new.strip():
                items.append({"text": text, "left": left, "orig": orig, "new": new, "right": right, "y": y})
    return items


SHOTS = (
    "Примеры:\n"
    "Фрагмент: Он [[быстро]] побежал домой. Замена: «быстро» → «стремительно». Ответ: ДА\n"
    "Фрагмент: Мы [[купили]] новый холодильник. Замена: «купили» → «приобрели». Ответ: ДА\n"
    "Фрагмент: Это очень [[трудная]] задача. Замена: «трудная» → «сложная». Ответ: ДА\n"
    "Фрагмент: Она налила [[чай]] в чашку. Замена: «чай» → «молоко». Ответ: НЕТ\n"
    "Фрагмент: Он [[открыл]] окно. Замена: «открыл» → «разбил». Ответ: НЕТ\n"
    "Фрагмент: В комнате было [[тепло]]. Замена: «тепло» → «светло». Ответ: НЕТ\n\n"
)
PROMPTS = {
    "strict": (
        "Проверка замены слова.\n\nФрагмент: {left} [[{orig}]] {right}\nЗамена: «{orig}» → «{new}»\n\n"
        "Подходит ли замена: смысл фрагмента остался тем же, слово стоит в нужной форме, "
        "фраза звучит естественно? Если есть сомнение, ответь НЕТ.\nОтвет: ДА или НЕТ."
    ),
    "pair": (
        "Исходная фраза: {left} {orig} {right}\nНовая фраза: {left} {new} {right}\n\n"
        "Значат ли эти две фразы одно и то же, и грамотна ли новая? Если есть сомнение, ответь НЕТ.\n"
        "Ответ: ДА или НЕТ."
    ),
    "pair_mark": (
        "Исходная фраза: {left} [[{orig}]] {right}\nНовая фраза: {left} [[{new}]] {right}\n\n"
        "Значат ли эти две фразы одно и то же, и грамотна ли новая? Если есть сомнение, ответь НЕТ.\n"
        "Ответ: ДА или НЕТ."
    ),
    "pair_named": (
        "Исходная фраза: {left} {orig} {right}\nНовая фраза: {left} {new} {right}\n"
        "Заменено слово «{orig}» на «{new}».\n\n"
        "Значат ли эти две фразы одно и то же, и грамотна ли новая? Если есть сомнение, ответь НЕТ.\n"
        "Ответ: ДА или НЕТ."
    ),
    "meaning": (
        "Фрагмент: {left} [[{orig}]] {right}\nСлово «{orig}» заменили на «{new}».\n"
        "Сохранился ли смысл? Грамматику не оценивай. Синоним должен быть точным: "
        "близкое по теме, но другое слово — это НЕТ.\nОтвет: ДА или НЕТ."
    ),
    "shots": (
        "Проверь, можно ли заменить слово синонимом без потери смысла. Синоним должен быть точным: "
        "близкое по теме, но другое слово — это НЕТ.\n\n" + SHOTS +
        "Фрагмент: {left} [[{orig}]] {right}\nЗамена: «{orig}» → «{new}». Ответ:"
    ),
}


def score_of(model, prompt):
    body = json.dumps({"model": model, "stream": False, "keep_alive": "5m",
                       "messages": [{"role": "user", "content": prompt}],
                       "logprobs": True, "top_logprobs": 10,
                       "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 6}}).encode()
    d = json.loads(urllib.request.urlopen(urllib.request.Request(
        "http://127.0.0.1:11434/api/chat", data=body, headers={"Content-Type": "application/json"}),
        timeout=120).read())
    for entry in d.get("logprobs") or []:
        if not entry["token"].strip():
            continue  # skip a leading space token
        yes = no = 0.0
        for t in entry["top_logprobs"]:
            w = t["token"].strip().upper().replace("Ё", "Е").rstrip(".,:!")
            if w in ("ДА", "YES"):
                yes += math.exp(t["logprob"])
            elif w in ("НЕТ", "НЕ", "NO"):
                no += math.exp(t["logprob"])
        return math.log(yes + 1e-9) - math.log(no + 1e-9), yes + no
    return 0.0, 0.0


def auc(pos, neg):
    if not pos or not neg:
        return float("nan")
    gt = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return gt / (len(pos) * len(neg))


def at_threshold(items, scores, thr):
    tp = sum(1 for it, s in zip(items, scores) if it["y"] == "G" and s >= thr)
    fp = sum(1 for it, s in zip(items, scores) if it["y"] != "G" and s >= thr)
    g = sum(1 for it in items if it["y"] == "G")
    return tp / max(1, g), tp / max(1, tp + fp), tp + fp


def main():
    model = sys.argv[1]
    names = sys.argv[2].split(",") if len(sys.argv) > 2 else list(PROMPTS)
    items = load_set()
    dev = [i for i in items if i["text"] == "space"]
    test = [i for i in items if i["text"] == "web"]
    print(f"{model}: dev={len(dev)} test={len(test)}  base precision dev={sum(i['y']=='G' for i in dev)/len(dev):.0%} test={sum(i['y']=='G' for i in test)/len(test):.0%}", flush=True)
    store = {}
    for name in names:
        t0 = time.time()
        sc = {}
        for part, subset in (("dev", dev), ("test", test)):
            sc[part] = []
            for it in subset:
                pr = PROMPTS[name].replace("{left}", it["left"]).replace("{right}", it["right"]) \
                    .replace("{orig}", it["orig"]).replace("{new}", it["new"])
                sc[part].append(score_of(model, pr)[0])
        store[name] = sc
        ad = auc([s for s, i in zip(sc["dev"], dev) if i["y"] == "G"], [s for s, i in zip(sc["dev"], dev) if i["y"] != "G"])
        at = auc([s for s, i in zip(sc["test"], test) if i["y"] == "G"], [s for s, i in zip(sc["test"], test) if i["y"] != "G"])
        print(f"  {name:8s} AUC dev={ad:.3f} test={at:.3f}   ({time.time()-t0:.0f}s)", flush=True)
    json.dump(store, open(os.path.join(HERE, f"validator2_{slug(model)}.json"), "w"), ensure_ascii=False)
    try:
        urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:11434/api/generate",
            data=json.dumps({"model": model, "keep_alive": 0}).encode(), headers={"Content-Type": "application/json"}), timeout=30).read()
    except Exception:
        pass


main()
