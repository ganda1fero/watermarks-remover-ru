"""Сравнение стратегий слоя B на одних и тех же текстах (всё локально, через Ollama).

usage: python compare_strategies.py MODEL TEXT[,TEXT...] [STRATEGY[,STRATEGY...]]
  TEXT: имя файла в experiments/ (test_text.txt, test_text_web.txt)
  STRATEGY: имена из STRATEGIES ниже (по умолчанию все)

Для каждой пары «текст + стратегия» печатает: потерянные и новые числа, потерянные имена, расхождение с
оригиналом, долю абзацев, оставшихся без изменений, изменение длины, время и счётчики шагов. Результат каждого
запуска сохраняется в compare_out/<текст>__<стратегия>.txt (читать глазами).
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

STRATEGIES = {
    # (стратегия, проверка смысла замен)
    "A_paraphrase_swap": ("paraphrase@0.8,swap@0.2", True),   # прежняя схема по умолчанию
    "B_refresh": ("refresh@0.8", None),                        # новая: освежить только «машинные» абзацы
    "C_refresh_swap": ("refresh@0.8,swap@0.2", True),          # новая + замена слов
    "D_swap_only": ("swap@0.2", True),                         # только замена слов
}
NUM = re.compile(r"\d[\d.,]*\d|\d")
CAP = re.compile(r"(?<![.!?]\s)(?<!^)\b[А-ЯЁA-Z][а-яёa-z\-]{2,}")


def main() -> None:
    model = sys.argv[1]
    texts = sys.argv[2].split(",")
    names = sys.argv[3].split(",") if len(sys.argv) > 3 else list(STRATEGIES)
    os.environ.update({"WATERMARKS_REWRITE_KEEP_ALIVE": "30s", "WATERMARKS_REWRITE_NUM_CTX": "4096",
                       "WATERMARKS_SWAP_CTX_TOKENS": "1200"})
    os.makedirs(os.path.join(HERE, "compare_out"), exist_ok=True)
    rows = []
    for tname in texts:
        text = open(os.path.join(HERE, tname), encoding="utf-8").read().strip()
        orig_paras = text.split("\n\n")
        for sname in names:
            strategy, verify = STRATEGIES[sname]
            t0 = time.time()
            out, st = r.apply_strategy(
                text, r.parse_strategy(strategy), backend="ollama", model=model,
                base_url="http://127.0.0.1:11434", api_key=None, timeout=900, swap_verify=verify,
            )
            sec = time.time() - t0
            paras = out.split("\n\n")
            same = sum(1 for a, b in zip(orig_paras, paras) if a.strip() == b.strip())
            n_in, n_out = set(NUM.findall(text)), set(NUM.findall(out))
            c_in, c_out = {w[:5].lower() for w in CAP.findall(text)}, {w[:5].lower() for w in CAP.findall(out)}
            steps = {s["tactic"]: {k: v for k, v in s.items() if k in ("refresh", "swap")} for s in st["steps"]}
            fresh = steps.get("refresh", {}).get("refresh", {})
            swap = steps.get("swap", {}).get("swap", {})
            row = {
                "text": tname, "strategy": sname, "sec": round(sec), "paragraphs": (len(orig_paras), len(paras)),
                "unchanged_paragraphs": same,
                "numbers_lost": sorted(n_in - n_out), "numbers_new": sorted(n_out - n_in),
                "names_lost": sorted(c_in - c_out)[:8],
                "divergence": round(r._lexical_divergence(text, out), 2),
                "words": (len(text.split()), len(out.split())),
                "refresh": {k: fresh.get(k) for k in ("eligible", "flagged", "rewritten", "kept_guard")} if fresh else None,
                "swap": {k: swap.get(k) for k in ("target", "accepted", "verify_rejected", "errors")} if swap else None,
            }
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if fresh:
                for d in fresh.get("details", []):
                    if d["kept_original"]:
                        print("   абзац", d["index"], "оставлен исходным:", d["kept_original"], flush=True)
            slug = f"{os.path.splitext(tname)[0]}__{sname}.txt"
            open(os.path.join(HERE, "compare_out", slug), "w", encoding="utf-8").write(out)
    json.dump(rows, open(os.path.join(HERE, "compare_out", "summary.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    try:
        import urllib.request
        urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:11434/api/generate", data=json.dumps({"model": model, "keep_alive": 0}).encode(),
            headers={"Content-Type": "application/json"}), timeout=10)
    except Exception:
        pass
    print("ГОТОВО", flush=True)


main()
