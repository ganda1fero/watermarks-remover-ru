"""Подбор порога проверки смысла для каждой модели по оценкам из verify_scores_*.json (их пишет embed_eval.py).

usage: python pick_thresholds.py [ЦЕЛЕВАЯ_ТОЧНОСТЬ]      (по умолчанию 0.80)

Для каждой модели печатает таблицу порогов: какая доля принятых замен годная (точность) и сколько годных
замен проходит (полнота). Рекомендуемый порог: наименьший, при котором точность достигает цели; если цели не
достичь, берётся порог с наибольшей точностью при полноте не ниже 15%.
"""
import glob
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = float(sys.argv[1]) if len(sys.argv) > 1 else 0.80


def table(scores, good):
    total_good = sum(good)
    rows = []
    for thr in [t / 2 for t in range(-10, 61)]:  # от -5 до 30 с шагом 0,5
        acc = [g for s, g in zip(scores, good) if s >= thr]
        if not acc:
            continue
        rows.append((thr, sum(acc) / len(acc), sum(acc) / total_good, len(acc)))
    return rows


def pick(rows):
    for thr, prec, rec, n in rows:
        if prec >= TARGET and rec >= 0.15:
            return thr, prec, rec, n
    ok = [r for r in rows if r[2] >= 0.15]
    return max(ok, key=lambda r: r[1]) if ok else rows[-1]


def main():
    out = {}
    for path in sorted(glob.glob(os.path.join(HERE, "verify_scores_*.json"))):
        d = json.load(open(path, encoding="utf-8"))
        rows = table(d["scores"], d["good"])
        base = sum(d["good"]) / len(d["good"])
        print(f"\n== {d['model']}  (без проверки годных {base:.0%} из {len(d['good'])})")
        print("  порог  точность  полнота  принято")
        for thr, prec, rec, n in rows:
            if thr in (-2, 0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 25):
                print(f"  {thr:5.1f}  {prec:7.0%}  {rec:7.0%}  {n:7d}")
        thr, prec, rec, n = pick(rows)
        print(f"  -> рекомендуемый порог {thr:g}: точность {prec:.0%}, пропускает {rec:.0%} годных")
        out[d["model"]] = {"verify_min": thr, "precision": round(prec, 3), "recall": round(rec, 3)}
    json.dump(out, open(os.path.join(HERE, "verify_thresholds.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


main()
