"""Tests for the `swap` tactic of rewrite_text (word-by-word synonym swap).

The LLM is replaced by a fake `_generate_once`, so no Ollama is needed. Checks that
depend on the Russian dictionary (pymorphy3) are skipped when it is not installed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "service" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rewrite_text as r

TEXT = (
    "Космос остаётся главной целью современных исследований. Учёные предполагают, что "
    "миллиарды лет назад на поверхности красной планеты существовали реки и озёра, но "
    "выяснить, зарождалась ли там жизнь, пока не удалось. В 1957 году запустили первый "
    "спутник Земли, и люди слушали его радиосигналы по всему миру."
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """No leftover settings from the developer's shell; dictionary checks off by default."""
    for name in list(os.environ):
        if name.startswith(("WATERMARKS_SWAP_", "WATERMARKS_REWRITE_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WATERMARKS_SWAP_MORPH", "0")


def _asked_word(prompt: str) -> str:
    return prompt.split("Слово: «")[1].split("»")[0]


def _run(monkeypatch, reply, *, text: str = TEXT, level: float = 0.2, backend: str = "x"):
    """Run _swap_words with a fake model; `reply(word)` returns the model's answer."""
    asked: list[str] = []

    def fake(backend_, base_url, model, api_key, prompt, timeout, temperature, effort,
             keep_alive=None, max_tokens=None):
        word = _asked_word(prompt)
        asked.append(word)
        return reply(word)

    monkeypatch.setattr(r, "_generate_once", fake)
    out, stats = r._swap_words(
        text, level, backend=backend, base_url="http://x", model="m", api_key=None,
        timeout=5, temperature=0.9, reasoning_effort=None,
    )
    return out, stats, asked


def _tokens(s: str) -> list[str]:
    return r._MLM_TOKEN_RE.split(s)


def test_quota_is_met_and_only_words_change(monkeypatch):
    out, stats, _ = _run(monkeypatch, lambda w: "синоним", level=0.3)
    a, b = _tokens(TEXT), _tokens(out)
    assert len(a) == len(b)  # punctuation and spacing are never touched
    changed = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    assert changed and all(a[i].isalpha() and b[i] == "синоним" for i in changed)
    assert stats["accepted"] == len(changed) == stats["target"]
    assert stats["warnings"] == []


def test_words_near_numbers_and_dates_are_never_asked(monkeypatch):
    _, stats, asked = _run(monkeypatch, lambda w: "синоним", level=1.0)
    assert "году" not in asked  # a date word
    assert "запустили" not in asked  # within 2 words of "1957"
    assert stats["skipped_near_number"] >= 1


def test_refused_words_are_remembered_and_the_quota_warning_is_reported(monkeypatch):
    out, stats, asked = _run(monkeypatch, lambda w: "NULL", level=0.3)
    assert out == TEXT and stats["accepted"] == 0
    assert stats["refused"] == stats["calls"] == len(asked)
    assert len(asked) <= stats["candidates"]  # nobody was asked twice
    assert any("quota not met" in w for w in stats["warnings"])


def test_adjacent_words_are_not_both_replaced(monkeypatch):
    out, _, _ = _run(monkeypatch, lambda w: "синоним", level=1.0)
    words = [t for t in _tokens(out) if any(c.isalnum() for c in t)]
    assert all(not (a == "синоним" and b == "синоним") for a, b in zip(words, words[1:]))


def test_invalid_replies_are_rejected(monkeypatch):
    # Latin letters inside a Cyrillic word, the same word, and a digit are all refused.
    for bad in ("sиноним", "ЖИЗНЬ", "слово1"):
        out, stats, _ = _run(monkeypatch, lambda w, bad=bad: bad if bad != "ЖИЗНЬ" else w)
        assert out == TEXT and stats["accepted"] == 0 and stats["invalid"] > 0


def test_valid_swap_rules():
    assert r._valid_swap("космос", "вселенная") == "вселенная"
    assert r._valid_swap("космос", "космос") is None
    assert r._valid_swap("космос", "сkeptиками") is None  # script mixing
    assert r._valid_swap("космос", "три слова здесь") is None
    assert r._valid_swap("space", "cosmos") == "cosmos"
    assert r._valid_swap("space", "космос") is None


def test_context_window_shrinks_to_the_budget_with_a_warning(monkeypatch):
    monkeypatch.setenv("WATERMARKS_SWAP_CTX_TOKENS", "100")
    _, stats, _ = _run(monkeypatch, lambda w: "синоним")
    assert stats["ctx_shrunk"] > 0
    assert any("prompt budget" in w for w in stats["warnings"])


def test_meaning_check_rejects_low_scores(monkeypatch):
    monkeypatch.setenv("WATERMARKS_SWAP_VERIFY", "1")
    monkeypatch.setenv("WATERMARKS_SWAP_VERIFY_MIN", "12")
    scores = iter([-5.0, -5.0, 20.0] + [20.0] * 200)
    monkeypatch.setattr(r, "_verify_score", lambda *a, **k: next(scores))
    out, stats, _ = _run(monkeypatch, lambda w: "синоним", backend="ollama")
    assert stats["verify"] is True and stats["verify_rejected"] == 2
    assert stats["accepted"] == stats["target"]  # rejected words were replaced by other candidates
    assert stats["verify_calls"] == stats["accepted"] + 2


def test_meaning_check_is_skipped_for_other_backends(monkeypatch):
    monkeypatch.setenv("WATERMARKS_SWAP_VERIFY", "1")
    _, stats, _ = _run(monkeypatch, lambda w: "синоним", backend="openai-compatible")
    assert stats["verify"] is False
    assert any("needs the ollama backend" in w for w in stats["warnings"])


def test_verify_score_uses_logprobs_and_falls_back_to_text(monkeypatch):
    yes_no = {"logprobs": [{"token": " ДА", "top_logprobs": [
        {"token": " ДА", "logprob": -0.01}, {"token": " НЕТ", "logprob": -6.0}]}]}
    monkeypatch.setattr(r, "_http_json", lambda *a, **k: yes_no)
    assert 5.5 < r._verify_score("http://x", "m", "p", 5, None) < 6.5
    monkeypatch.setattr(r, "_http_json", lambda *a, **k: {"message": {"content": "НЕТ"}})
    assert r._verify_score("http://x", "m", "p", 5, None) == -30.0


def test_apply_strategy_reports_swap_stats(monkeypatch):
    monkeypatch.setattr(
        r, "_generate_once",
        lambda backend, base_url, model, api_key, prompt, *a, **k: "синоним",
    )
    out, info = r.apply_strategy(
        TEXT, [("swap", 0.2)], backend="ollama", model="m", base_url="http://x", api_key=None,
    )
    step = info["steps"][0]
    assert step["tactic"] == "swap" and step["swap"]["accepted"] >= 1
    assert isinstance(step["swap"]["warnings"], list) and out != TEXT


def test_dictionary_and_form_checks():
    pytest.importorskip("pymorphy3")
    morph = r._get_morph()
    assert morph is not None
    assert not morph.word_is_known("человекество")  # a made-up word
    assert r._form_ok("простые", "прямые")
    assert not r._form_ok("году", "год")  # case/number changed
    assert not r._form_ok("человек", "человека")
    assert not r._form_ok("самые", "максимально")  # part of speech changed


def test_swap_rejects_made_up_words_when_the_dictionary_is_on(monkeypatch):
    pytest.importorskip("pymorphy3")
    monkeypatch.setenv("WATERMARKS_SWAP_MORPH", "1")
    out, stats, _ = _run(monkeypatch, lambda w: "ыыыы")
    assert out == TEXT and stats["accepted"] == 0 and stats["dict_rejected"] > 0


def test_verify_threshold_comes_from_model_profile(monkeypatch, tmp_path):
    import json

    import rewrite_text as r

    prof = tmp_path / "p.json"
    prof.write_text(json.dumps({"profiles": [{"model": "m1", "verify_min": 3}]}), encoding="utf-8")
    monkeypatch.setenv("WATERMARKS_MODEL_PROFILES_FILE", str(prof))
    monkeypatch.delenv("WATERMARKS_SWAP_VERIFY_MIN", raising=False)
    assert r._verify_min_for("m1") == 3.0
    assert r._verify_min_for("other") == 12.0  # unknown model: the default
    assert r._has_profile_threshold("m1") and not r._has_profile_threshold("other")
    monkeypatch.setenv("WATERMARKS_SWAP_VERIFY_MIN", "7")  # an explicit value wins
    assert r._verify_min_for("m1") == 7.0


def test_paraphrase_keeps_paragraph_count_and_separators(monkeypatch):
    monkeypatch.delenv("WATERMARKS_PARAPHRASE_PER_PARAGRAPH", raising=False)
    text = "Короткая строка.\n\nЭто длинный абзац из достаточного числа слов для обработки.\n\n\nЕщё один длинный абзац из достаточного числа слов тут."
    parts = r._paragraph_parts(text)
    assert parts is not None and r._paragraph_parts("один абзац без разрывов") is None
    seen = []

    def gen(piece, keep):
        seen.append(piece)
        # the model returns two paragraphs for the second piece: they must be joined into one
        return "ПЕРЕПИСАНО раз.\n\nПЕРЕПИСАНО два." if "Ещё" in piece else "ПЕРЕПИСАНО " + piece

    out, st = r._paraphrase_paragraphs(parts, gen, keep_model=False, final_keep=None, base_url="", model="m")
    assert len(seen) == 2 and st["kept_short"] == 1 and st["rewritten"] == 2 and st["joined"] == 1
    assert out.split("\n\n")[0] == "Короткая строка."  # short paragraph untouched
    assert out.count("\n\n\n") == 1 and "ПЕРЕПИСАНО раз. ПЕРЕПИСАНО два." in out  # same separators, joined reply
    assert len([p for p in r._PARA_SPLIT.split(out)[::2] if p.strip()]) == 3

    def broken(piece, keep):
        raise RuntimeError("down")

    out2, st2 = r._paraphrase_paragraphs(parts, broken, keep_model=False, final_keep=None, base_url="", model="m")
    assert out2 == text and st2["kept_failed"] == 2


def test_keep_after_overrides_the_final_keep_alive(monkeypatch):
    sent = []

    def fake_http(url, payload, headers, timeout):
        sent.append(payload)
        return {"message": {"content": "NULL"}, "logprobs": []}

    monkeypatch.setattr(r, "_http_json", fake_http)
    monkeypatch.setenv("WATERMARKS_REWRITE_KEEP_ALIVE", "0")
    monkeypatch.setenv("WATERMARKS_SWAP_MORPH", "0")
    text = "Это достаточно длинный текст для проверки замены нескольких слов в одном предложении здесь."
    r._swap_words(text, 0.2, backend="ollama", base_url="http://127.0.0.1:1", model="m", api_key=None,
                  timeout=5, temperature=0.3, reasoning_effort=None, keep_after="10m")
    assert sent[-1].get("keep_alive") == "10m" and "messages" not in sent[-1]  # final call only sets keep_alive
