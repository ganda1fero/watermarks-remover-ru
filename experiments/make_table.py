"""Rebuild swap_experiments.md from swap_runs.jsonl (+ swap_grades.json, filled by hand).

Grade keys: "model|text|config" for v1 (legacy), "ver|model|text|config" for later versions.
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
runs = [json.loads(l) for l in open(os.path.join(HERE, "swap_runs.jsonl"), encoding="utf-8")]
gp = os.path.join(HERE, "swap_grades.json")
grades = json.load(open(gp, encoding="utf-8")) if os.path.exists(gp) else {}
CFG = {"ru_n3": "RU, n=3", "ru_n5": "RU, n=5", "ru_n10": "RU, n=10", "ru_n15": "RU, n=15",
       "ru_n20": "RU, n=20", "ru_n25": "RU, n=25", "ru_n30": "RU, n=30",
       "ru_s10": "RU-short, n=10", "en_n10": "EN, n=10", "rujson_n10": "RU-JSON, n=10", "ruhl_n10": "RU+HL, n=10"}
order = {k: i for i, k in enumerate(CFG)}
runs.sort(key=lambda j: (j.get("ver", "v1"), j["model"], j["text"], order.get(j["config"], 99)))


def grade_of(j):
    ver = j.get("ver", "v1")
    for key in (f"{ver}|{j['model']}|{j['text']}|{j['config']}",
                f"{j['model']}|{j['text']}|{j['config']}" if ver == "v1" else None):
        if key and key in grades:
            return grades[key]
    return {}


lines = [
    "# Эксперименты: пословный swap (окно ±n слов)",
    "",
    "Вход — исходный текст (без перефразирования), цель — 20% слов-кандидатов, температура 0,3, num_ctx 4096.",
    "Тексты: space (космос, 1958 токенов), web (веб-разработка).",
    "",
    "**Версии кода:** v1 — без правил для дат/чисел и без словаря; v2 — слова рядом с числами, месяцы и «год/лет»",
    "не заменяются, замена проверяется по словарю pymorphy3 (слова из самого текста допускаются) и по",
    "грамматической форме. Кандидатов в v2 меньше, поэтому «цель» (20% от них) не сопоставима с v1 напрямую.",
    "",
    "Колонки: **принято/цель** — выполнение квоты; **отказ** — модель ответила NULL; **негодн.** — ответ отбракован",
    "по формату (латиница, не одно слово); **слов.** / **форма** — отбраковано словарём / проверкой формы (только v2);",
    "**вызовов** — число запросов; **~ток.** — макс. оценка размера промпта; **суфф.*** — доля замен с тем же",
    "окончанием (последние 2 буквы), грубый признак формы;",
    "**оценка вручную** на 15 случайных заменах: ✓ годная замена / Ф неверная форма / Н несуществующее слово / С потеря смысла.",
    "",
    "| Вер. | Модель | Текст | Конфиг | принято/цель | отказ | негодн. | слов. | форма | вызовов | сек | ~ток. | суфф.* | ✓ | Ф | Н | С |",
    "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
]
for j in runs:
    ver = j.get("ver", "v1")
    if "error" in j:
        lines.append(f"| {ver} | {j['model']} | {j['text']} | {CFG.get(j['config'], j['config'])} | ОШИБКА: {j['error'][:60]} |||||||||||| ")
        continue
    g = grade_of(j)
    cells = [g.get(k, "") for k in ("G", "F", "N", "S")]
    dr = j.get("dict_rejected", "–") if ver != "v1" else "–"
    fr = j.get("form_rejected", "–") if ver != "v1" else "–"
    nb = j.get("neighbors", 0)
    extra = (f" +k={nb} {j.get('neighbor_mode')} (ц/л/п/об {j.get('pat_center')}/{j.get('pat_left')}/"
             f"{j.get('pat_right')}/{j.get('pat_both')}, сл.{j.get('span_mismatch')}, сос.{j.get('neighbor_rejected')})") if nb else ""
    lines.append(
        f"| {ver} | {j['model']} | {j['text']} | {CFG.get(j['config'], j['config'])}{extra} | {j['accepted']}/{j['target']} | "
        f"{j['refused']} | {j['invalid']} | {dr} | {fr} | {j['calls']} | {j['sec']} | {j['est_tokens_max']} | "
        f"{j['suffix2_match']} | " + " | ".join(str(c) for c in cells) + " |"
    )
open(os.path.join(HERE, "swap_experiments.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("\n".join(lines))
