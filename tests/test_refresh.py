"""refresh: переписываются только «машинные» абзацы, остальные остаются как есть."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service" / "scripts"))

import rewrite_text as r  # noqa: E402

NATURAL = (
    "Молодая рыжая собака, помесь таксы с дворняжкой, очень похожая мордой на лисицу, бегала взад и вперёд "
    "по тротуару и беспокойно оглядывалась по сторонам. Изредка она останавливалась и, плача, приподнимая "
    "то одну озябшую лапу, то другую, старалась дать себе отчёт: как это могло случиться, что она заблудилась?"
)
MACHINE = (
    "Данная система является важным инструментом осуществления контроля. Таким образом, внедрение данной "
    "системы обеспечивает эффективное использование ресурсов. Кроме того, необходимо отметить, что данная "
    "система позволяет осуществлять комплексное управление процессами в рамках организации."
)


def test_machine_text_scores_higher_than_natural_prose():
    assert r.paragraph_machine_score(NATURAL)["score"] < 0.5
    assert r.paragraph_machine_score(MACHINE)["score"] > 2.0


def test_guard_rejects_lost_numbers_names_and_big_length_changes():
    orig = "Нил Армстронг в 1969 году высадился на Луне, а площадь склада составляла 1 350 м2 в тот день тоже."
    assert r._refresh_guard(orig, orig) is None
    assert "numbers" in r._refresh_guard(orig, orig.replace("1969", "тот"))
    assert r._refresh_guard(orig, orig.replace("1 350", "1350")) is None  # thousands separator is not a loss
    assert "names" in r._refresh_guard(orig, orig.replace("Армстронг", "астронавт"))
    assert "length" in r._refresh_guard(orig, "Нил Армстронг 1969 Луне 1 350 м2.")
    assert "language" in r._refresh_guard(orig, "Neil Armstrong landed on the Moon in 1969 and the warehouse was 1 350 m2 that day too.")


def test_refresh_rewrites_only_flagged_paragraphs_and_keeps_originals_on_guard_failure(monkeypatch):
    monkeypatch.setenv("WATERMARKS_REFRESH_MIN_WORDS", "10")
    second = MACHINE + " Число пунктов равно 42."
    text = NATURAL + "\n\n" + MACHINE + "\n\n" + second
    parts = r._paragraph_parts(text)
    calls = []

    def gen(piece, keep):
        calls.append(piece)
        if "42" in piece:  # the model drops the number: this rewrite must be thrown away
            return "Система помогает управлять процессами и ресурсами организации лучше. Это удобно."
        return piece.replace("Данная система является", "Эта система служит").replace("Таким образом, ", "")

    out, st = r._refresh_paragraphs(parts, gen, share=1.0, keep_model=False, final_keep=None, base_url="", model="m")
    assert NATURAL in out  # the natural paragraph was never sent to the model
    assert all(NATURAL not in c for c in calls)
    assert "Эта система служит" in out
    assert second in out  # guard failure: the original paragraph stays
    assert st["flagged"] == 2 and st["rewritten"] == 1 and st["kept_guard"] == 1
    assert len([p for p in r._PARA_SPLIT.split(out)[::2] if p.strip()]) == 3


def test_share_limits_the_number_of_rewritten_paragraphs(monkeypatch):
    monkeypatch.setenv("WATERMARKS_REFRESH_MIN_WORDS", "10")
    text = "\n\n".join([MACHINE, MACHINE.replace("Данная", "Эта"), NATURAL, NATURAL.replace("Молодая", "Старая")])
    parts = r._paragraph_parts(text)
    out, st = r._refresh_paragraphs(
        parts, lambda piece, keep: piece.replace("Таким образом, ", ""), share=0.25,
        keep_model=False, final_keep=None, base_url="", model="m",
    )
    assert st["eligible"] == 4 and st["flagged"] == 1  # ceil(0.25 * 4) = 1 of the two machine-like ones


def test_refresh_is_a_known_strategy_step():
    assert ("refresh", 0.8) in r.parse_strategy("refresh@0.8,swap@0.2")
    assert "refresh" in r.LLM_TACTICS


def test_guard_ignores_superscript_and_thousands_formatting():
    orig = "Склад площадью 1 350,00 м^2 заполнен полностью, а годовые издержки составили 5 379,37 д. е. в год."
    new = "Склад площадью 1350,00 м² заполнен полностью, годовые издержки составили 5379,37 д. е. за год."
    assert r._refresh_guard(orig, new) is None
