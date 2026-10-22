"""Слепое сравнение двух схем слоя B на одном тексте.

usage: python blind_ab.py MODEL INPUT.txt OUT_DIR

Прогоняет старую схему (paraphrase@0.8,swap@0.2) и новую (refresh@0.8,swap@0.2), обе с проверкой смысла
замен, затем одинаково пропускает результат через слой A. Файлы записываются как вариант_1.txt и
вариант_2.txt в случайном порядке; какой из них какой, записано в OUT_DIR/ключ.json (не показывать до выбора).
"""
import json
import os
import random
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "service", "scripts"))
import rewrite_text as r  # noqa: E402
from text_unicode import clean_text  # noqa: E402

SCHEMES = {"старая": "paraphrase@0.8,swap@0.2", "новая": "refresh@0.8,swap@0.2"}


def main() -> None:
    model, src, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(out_dir, exist_ok=True)
    os.environ.update({"WATERMARKS_REWRITE_KEEP_ALIVE": "30s", "WATERMARKS_REWRITE_NUM_CTX": "4096",
                       "WATERMARKS_SWAP_CTX_TOKENS": "1200"})
    text = open(src, encoding="utf-8").read().strip()
    results = {}
    for name, strategy in SCHEMES.items():
        t0 = time.time()
        out, st = r.apply_strategy(
            text, r.parse_strategy(strategy), backend="ollama", model=model,
            base_url="http://127.0.0.1:11434", api_key=None, timeout=900, swap_verify=True,
        )
        results[name] = clean_text(out)[0]  # layer A, the same for both
        print(f"{name}: готово за {time.time() - t0:.0f} с", flush=True)
    order = list(SCHEMES)
    random.SystemRandom().shuffle(order)
    key = {}
    for i, name in enumerate(order, 1):
        open(os.path.join(out_dir, f"вариант_{i}.txt"), "w", encoding="utf-8", newline="").write(results[name])
        key[f"вариант_{i}"] = name
    json.dump(key, open(os.path.join(out_dir, "ключ.json"), "w", encoding="utf-8"), ensure_ascii=False)
    try:
        import urllib.request
        urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:11434/api/generate", data=json.dumps({"model": model, "keep_alive": 0}).encode(),
            headers={"Content-Type": "application/json"}), timeout=10)
    except Exception:
        pass
    print("ГОТОВО: файлы вариант_1.txt и вариант_2.txt", flush=True)


main()
