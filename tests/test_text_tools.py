"""Tests for the text tooling endpoints: /layer-a, /estimate, /patch and report.layer_b edits.

The rewrite model is replaced by a fake, so no Ollama is needed.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "service" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import estimate
import rewrite_text
import server

# conftest replaces server._apply_layer_b with a no-op for every test; the real one is
# captured here, at import time, for the end-to-end tests below.
_REAL_APPLY_LAYER_B = server._apply_layer_b

TEXT = (
    "Космос остаётся главной целью современных исследований. Учёные предполагают, что "
    "миллиарды лет назад на поверхности красной планеты существовали реки и озёра, но "
    "выяснить, зарождалась ли там жизнь, пока не удалось. В 1957 году запустили первый "
    "спутник Земли, и люди слушали его радиосигналы по всему миру."
)


def _b64(s: str | bytes) -> str:
    return base64.b64encode(s.encode("utf-8") if isinstance(s, str) else s).decode("ascii")


def _unb64(s: str) -> str:
    return base64.b64decode(s).decode("utf-8")


def _post(conn: http.client.HTTPConnection, path: str, payload: dict) -> tuple[int, dict]:
    conn.request("POST", path, body=json.dumps(payload).encode("utf-8"),
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = resp.read()
    return resp.status, json.loads(data) if data else {}


@pytest.fixture(scope="module")
def conn() -> http.client.HTTPConnection:
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
    yield c
    c.close()
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for name in list(os.environ):
        if name.startswith(("WATERMARKS_SWAP_", "WATERMARKS_REWRITE_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WATERMARKS_SWAP_MORPH", "0")
    monkeypatch.setenv("WATERMARKS_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("WATERMARKS_REWRITE_BACKEND", "ollama")
    monkeypatch.setenv("WATERMARKS_REWRITE_MODEL", "main-model")
    monkeypatch.setenv("WATERMARKS_REWRITE_BASE_URL", "http://127.0.0.1:11434")


# ---------------------------------------------------------------- /layer-a


def test_layer_a_removes_invisible_characters_and_does_not_rewrite(conn):
    dirty = "Привет​, мир­! Это проверка."
    status, body = _post(conn, "/layer-a", {"file": _b64(dirty), "name": "x.txt"})
    assert status == 200 and body["kind"] == "text" and body["report"]["layer"] == "A"
    cleaned = _unb64(body["cleaned"])
    assert "​" not in cleaned and "­" not in cleaned
    assert cleaned == "Привет, мир! Это проверка."  # only the invisible characters are gone


def test_layer_a_refuses_non_text(conn):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    status, body = _post(conn, "/layer-a", {"file": _b64(png), "name": "x.png"})
    assert status == 400 and "text only" in body["error"]


# ---------------------------------------------------------------- /estimate


def test_estimate_reports_time_chunks_and_installed_alternatives(conn, monkeypatch):
    monkeypatch.setattr(server, "_DEFAULT_STRATEGY", "swap@0.2")
    monkeypatch.setattr(estimate, "installed_models", lambda base_url: {"gemma3:4b"})
    status, body = _post(conn, "/estimate", {"file": _b64(TEXT * 20), "name": "x.txt"})
    assert status == 200 and body["ok"] is True
    assert body["words"] > 500 and body["expected_seconds"] > 0
    low, high = body["expected_range_seconds"]
    assert low < body["expected_seconds"] < high
    assert body["steps"][0]["tactic"] == "swap" and body["swap_plan"]["target"] > 0
    assert body["chunk_plan"]["chunks"] >= 2
    by_model = {a["model"]: a for a in body["alternatives"]}
    assert by_model["gemma3:4b"]["installed"] is True
    assert by_model["qwen2.5:14b"]["installed"] is False
    assert by_model["gemma3:4b"]["expected_seconds"] < by_model["qwen2.5:14b"]["expected_seconds"]
    assert body["basis"].startswith("default")
    assert body["model"] == "main-model" and body["model_source"] == "env"
    status, body = _post(
        conn, "/estimate",
        {"file": _b64(TEXT), "name": "x.txt", "options": {"rewrite_model": "gemma3:4b"}},
    )
    assert body["model"] == "gemma3:4b" and body["model_source"] == "options.rewrite_model"


def test_estimate_calibrates_on_real_runs(conn, monkeypatch):
    monkeypatch.setattr(server, "_DEFAULT_STRATEGY", "swap@0.2")
    monkeypatch.setattr(estimate, "installed_models", lambda base_url: None)
    first = _post(conn, "/estimate", {"file": _b64(TEXT * 10), "name": "x.txt"})[1]
    for _ in range(3):  # a machine that is 10x faster than the defaults
        estimate.record_run(
            "main-model", False,
            [{"tactic": "swap", "seconds": 5.0, "swap": {"accepted": 100}}],
        )
    again = _post(conn, "/estimate", {"file": _b64(TEXT * 10), "name": "x.txt"})[1]
    assert again["basis"].startswith("measured") and again["calibration_samples"] >= 3
    assert again["steps"][0]["seconds_per_swap"] == pytest.approx(0.05, rel=0.05)
    assert again["expected_seconds"] < first["expected_seconds"]
    assert again["alternatives"][0]["installed"] is None  # Ollama could not be asked


def test_estimate_prices_the_meaning_check_and_sizes_chunks_by_the_context_window(conn, monkeypatch):
    monkeypatch.setattr(server, "_DEFAULT_STRATEGY", "paraphrase@0.8,swap@0.2")
    monkeypatch.setattr(estimate, "installed_models", lambda base_url: None)
    big = TEXT * 30
    on = _post(conn, "/estimate", {"file": _b64(big), "name": "x.txt",
                                   "options": {"meaning_check": True}})[1]
    off = _post(conn, "/estimate", {"file": _b64(big), "name": "x.txt",
                                    "options": {"meaning_check": False}})[1]
    assert on["meaning_check"] is True and off["meaning_check"] is False
    assert on["expected_seconds_with_meaning_check"] > on["expected_seconds_without_meaning_check"]
    assert on["expected_seconds"] == on["expected_seconds_with_meaning_check"]
    assert off["expected_seconds"] == off["expected_seconds_without_meaning_check"]
    assert on["chunk_plan"]["chunk_words"] <= 470  # default 4096-token window
    monkeypatch.setenv("WATERMARKS_REWRITE_NUM_CTX", "8192")
    wider = _post(conn, "/estimate", {"file": _b64(big), "name": "x.txt"})[1]
    assert wider["chunk_plan"]["chunk_words"] > on["chunk_plan"]["chunk_words"]
    assert wider["chunk_plan"]["chunks"] < on["chunk_plan"]["chunks"]


def test_estimate_offers_every_layer_b_mode_with_ready_options(conn, monkeypatch):
    monkeypatch.setattr(server, "_DEFAULT_STRATEGY", "refresh@0.8,swap@0.2")
    monkeypatch.setattr(estimate, "installed_models", lambda base_url: None)
    monkeypatch.setenv("WATERMARKS_SWAP_VERIFY", "1")
    body = _post(conn, "/estimate", {"file": _b64(TEXT * 20), "name": "x.txt"})[1]
    modes = {m["id"]: m for m in body["modes"]}
    assert set(modes) == {
        "refresh_swap", "refresh_swap_fast", "refresh", "paraphrase", "paraphrase_swap", "paraphrase_swap_fast",
        "swap_only", "swap_only_fast",
    }
    assert modes["refresh_swap"]["options"] == {"strategy": "refresh@0.8,swap@0.2", "meaning_check": True}
    assert modes["refresh"]["options"] == {"strategy": "refresh@0.8"}
    # refresh rewrites only a part of the paragraphs, so it is cheaper than paraphrasing everything
    assert modes["refresh"]["expected_seconds"] < modes["paraphrase"]["expected_seconds"]
    assert modes["refresh_swap"]["expected_seconds"] < modes["paraphrase_swap"]["expected_seconds"]
    # relative_time counts from the fastest mode; adding the swap step makes paraphrasing longer, the check longer still
    assert min(m["relative_time"] for m in modes.values()) == 1.0
    assert (modes["paraphrase"]["expected_seconds"] < modes["paraphrase_swap_fast"]["expected_seconds"]
            < modes["paraphrase_swap"]["expected_seconds"])
    assert modes["paraphrase_swap"]["options"] == {"strategy": "paraphrase@0.8,swap@0.2", "meaning_check": True}
    assert modes["paraphrase"]["options"] == {"strategy": "paraphrase@0.8"}
    assert modes["swap_only"]["options"] == {"strategy": "swap@0.2", "meaning_check": True}
    assert modes["swap_only_fast"]["options"] == {"strategy": "swap@0.2", "meaning_check": False}
    assert modes["swap_only_fast"]["expected_seconds"] < modes["swap_only"]["expected_seconds"]
    assert [m["id"] for m in body["modes"] if m["is_current"]] == ["refresh_swap"]
    assert all(m["available"] for m in body["modes"])


def test_modes_with_the_meaning_check_are_unavailable_without_ollama(conn, monkeypatch):
    monkeypatch.setattr(server, "_DEFAULT_STRATEGY", "paraphrase@0.8,swap@0.2")
    monkeypatch.setenv("WATERMARKS_REWRITE_BACKEND", "openai-compatible")
    body = _post(conn, "/estimate", {"file": _b64(TEXT * 5), "name": "x.txt"})[1]
    modes = {m["id"]: m for m in body["modes"]}
    assert modes["paraphrase_swap"]["available"] is False and modes["paraphrase_swap"]["reason"]
    assert modes["swap_only"]["available"] is False and modes["swap_only_fast"]["available"]
    assert modes["paraphrase"]["available"] and modes["paraphrase_swap_fast"]["available"]


def test_a_mode_runs_through_clean_with_its_options(conn, monkeypatch):
    """Plain paraphrase: no swap step, so no list of swaps; the strategy option picks the mode."""
    monkeypatch.setattr(rewrite_text, "_generate_once", lambda *a, **k: "Переписанный текст.")
    monkeypatch.setattr(server, "_apply_layer_b", _REAL_APPLY_LAYER_B)
    monkeypatch.setattr(server, "_DEFAULT_STRATEGY", "paraphrase@0.8,swap@0.2")
    status, body = _post(conn, "/clean", {"file": _b64(TEXT), "name": "x.txt",
                                          "options": {"strategy": "paraphrase@0.8", "report_edits": True}})
    assert status == 200
    assert [s["tactic"] for s in body["report"]["layer_b"]["steps"]] == ["paraphrase"]
    assert _unb64(body["cleaned"]) == "Переписанный текст."


def test_meaning_check_option_overrides_the_environment(monkeypatch):
    monkeypatch.setenv("WATERMARKS_SWAP_VERIFY", "1")
    assert server._verify_enabled() is True
    assert server._verify_enabled({"meaning_check": False}) is False
    monkeypatch.setenv("WATERMARKS_SWAP_VERIFY", "0")
    assert server._verify_enabled({"meaning_check": True}) is True
    monkeypatch.setenv("WATERMARKS_REWRITE_BACKEND", "openai-compatible")
    assert server._verify_enabled({"meaning_check": True}) is False  # the check needs Ollama


def test_estimate_needs_text_and_a_strategy(conn, monkeypatch):
    monkeypatch.setattr(server, "_DEFAULT_STRATEGY", None)
    status, body = _post(conn, "/estimate", {"file": _b64(TEXT), "name": "x.txt"})
    assert status == 400 and "strategy" in body["error"]


# ---------------------------------------------------------------- /patch and apply_patch


def test_apply_patch_changes_only_the_listed_whole_words():
    text = "Он смотрел на красивые картины и красивые дома."
    first = text.index("картины")
    second = text.index("красивые", 20)
    new, applied, skipped = estimate.apply_patch(text, [
        {"offset": first, "old": "картины", "new": "полотна"},
        {"offset": second, "old": "красивые", "new": "нарядные"},
    ])
    assert new == "Он смотрел на красивые полотна и нарядные дома."
    assert applied == [0, 1] and skipped == []


def test_apply_patch_skips_mismatches_partial_words_and_overlaps():
    text = "Он смотрел на красивые картины."
    new, applied, skipped = estimate.apply_patch(text, [
        {"offset": 0, "old": "Она", "new": "Они"},  # the text there is different
        {"offset": text.index("красив"), "old": "красив", "new": "умн"},  # not a whole word
        {"offset": text.index("картины"), "old": "картины", "new": "полотна"},
        {"offset": text.index("картины") + 2, "old": "ртин", "new": "xxxx"},  # overlaps the previous
    ])
    assert new == "Он смотрел на красивые полотна."
    assert applied == [2]
    assert {s["index"] for s in skipped} == {0, 1, 3}


def test_apply_patch_validates_its_input():
    with pytest.raises(ValueError):
        estimate.apply_patch("текст", "not a list")
    with pytest.raises(ValueError):
        estimate.apply_patch("текст", [{"offset": "0", "old": "а", "new": "б"}])
    assert estimate.apply_patch("текст", [{"offset": 0, "old": "текст", "new": "две\nстроки"}])[2]


def test_patch_endpoint(conn):
    text = "Космос остаётся главной целью."
    status, body = _post(conn, "/patch", {
        "file": _b64(text), "name": "x.txt",
        "edits": [{"offset": text.index("главной"), "old": "главной", "new": "основной"}],
    })
    assert status == 200 and body["applied"] == 1 and body["skipped"] == []
    assert _unb64(body["cleaned"]) == "Космос остаётся основной целью."
    bad, err = _post(conn, "/patch", {"file": _b64(text), "name": "x.txt", "edits": "nope"})
    assert bad == 400 and "edits" in err["error"]


# ---------------------------------------------------------------- end to end through /clean


def test_clean_lists_swaps_and_a_patch_can_undo_them_exactly(conn, monkeypatch):
    models_seen: list[str] = []

    def fake(backend, base_url, model, api_key, prompt, timeout, temperature, effort,
             keep_alive=None, max_tokens=None):
        models_seen.append(model)
        return "синоним"

    monkeypatch.setattr(rewrite_text, "_generate_once", fake)
    monkeypatch.setattr(server, "_apply_layer_b", _REAL_APPLY_LAYER_B)
    monkeypatch.setattr(server, "_DEFAULT_STRATEGY", "swap@0.3")

    status, body = _post(conn, "/clean", {
        "file": _b64(TEXT), "name": "x.txt",
        "options": {"report_edits": True, "rewrite_model": "other-model"},
    })
    assert status == 200
    assert set(models_seen) == {"other-model"}  # the per-request model override was used
    step = body["report"]["layer_b"]["steps"][0]
    assert step["seconds"] >= 0 and step["words"] > 20
    swap = step["swap"]
    assert swap["offsets_valid"] is True and swap["edits"]
    assert body["report"]["timing"]["words"] > 20
    guards = body["report"]["guards"]  # cheap fact checks for the review step
    assert guards["numbers_lost"] == [] and guards["numbers_new"] == []
    assert guards["sentences"][0] == guards["sentences"][1] and guards["cyrillic_share"][1] > 0.9
    cleaned = _unb64(body["cleaned"])
    assert cleaned != TEXT
    for edit in swap["edits"]:
        assert cleaned[edit["offset"] : edit["offset"] + len(edit["new"])] == edit["new"]
        assert edit["left"] != "" or edit["right"] != ""

    # Undo every swap with /patch: the result must be the original text, nothing else touched.
    status, patched = _post(conn, "/patch", {
        "file": body["cleaned"], "name": "x.txt",
        "edits": [{"offset": e["offset"], "old": e["new"], "new": e["orig"]} for e in swap["edits"]],
    })
    assert status == 200 and patched["skipped"] == []
    assert _unb64(patched["cleaned"]) == TEXT


def test_clean_without_report_edits_has_no_edit_list(conn, monkeypatch):
    monkeypatch.setattr(rewrite_text, "_generate_once", lambda *a, **k: "синоним")
    monkeypatch.setattr(server, "_apply_layer_b", _REAL_APPLY_LAYER_B)
    monkeypatch.setattr(server, "_DEFAULT_STRATEGY", "swap@0.3")
    status, body = _post(conn, "/clean", {"file": _b64(TEXT), "name": "x.txt"})
    assert status == 200 and "edits" not in body["report"]["layer_b"]["steps"][0]["swap"]


def test_new_endpoints_are_in_the_openapi_spec(conn):
    conn.request("GET", "/openapi.json")
    spec = json.loads(conn.getresponse().read())
    for path in ("/layer-a", "/estimate", "/patch"):
        assert "post" in spec["paths"][path]
    options = spec["paths"]["/clean"]["post"]["requestBody"]["content"]["application/json"][
        "schema"]["properties"]["options"]["properties"]
    assert "report_edits" in options and "rewrite_model" in options


# ---------------------------------------------------------------- keep_model / unload


def test_unload_frees_the_configured_model(conn, monkeypatch):
    import rewrite_text

    calls = []
    monkeypatch.setattr(rewrite_text, "unload_model", lambda base, model: calls.append((base, model)) or True)
    status, body = _post(conn, "/unload", {})
    assert status == 200 and body["unloaded"] is True and body["model"] == "main-model"
    status, body = _post(conn, "/unload", {"options": {"rewrite_model": "gemma3:4b"}})
    assert body["model"] == "gemma3:4b" and calls[-1][1] == "gemma3:4b"
    monkeypatch.setenv("WATERMARKS_REWRITE_BACKEND", "print-prompt")
    status, body = _post(conn, "/unload", {})
    assert status == 200 and body["unloaded"] is False


def test_estimate_counts_one_model_load_when_keep_alive_is_long(conn, monkeypatch):
    monkeypatch.setattr(server, "_DEFAULT_STRATEGY", "swap@0.2")
    monkeypatch.setattr(estimate, "installed_models", lambda base_url: None)
    body = {"file": _b64(TEXT * 40), "name": "x.txt"}
    monkeypatch.setenv("WATERMARKS_REWRITE_KEEP_ALIVE", "0")
    short = _post(conn, "/estimate", body)[1]["chunk_plan"]
    monkeypatch.setenv("WATERMARKS_REWRITE_KEEP_ALIVE", "30s")
    long_ = _post(conn, "/estimate", body)[1]["chunk_plan"]
    assert short["chunks"] >= 2 and short["model_stays_loaded_between_chunks"] is False
    assert long_["model_stays_loaded_between_chunks"] is True
    assert long_["expected_seconds_if_chunked"] < short["expected_seconds_if_chunked"]
