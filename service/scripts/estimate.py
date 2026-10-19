"""Time estimates, run calibration and exact word patches for text cleaning.

Used by the HTTP service (`server.py`):

* ``estimate_text``  – how long a text clean will take and how to split it (``POST /estimate``);
* ``record_run``     – remember how fast real runs were, so estimates match this machine;
* ``apply_patch``    – replace exactly the listed words and nothing else (``POST /patch``).

Everything here is stdlib only. Calibration lives in a small JSON file
(``WATERMARKS_STATE_FILE``, default: a file in the system temp directory).
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()

# Measured on the author's laptop (RTX 3060 6 GB, YandexGPT-5-Lite 8B through Ollama).
# They are only the starting point: real runs overwrite them (exponential average).
DEFAULTS: dict[str, float] = {
    "model_load_seconds": 20.0,
    "swap_seconds_per_swap_verify": 1.15,  # about 3.5 model requests per swap incl. the meaning check
    "swap_seconds_per_swap_plain": 0.47,  # about 1.2 requests per swap
    # whole-text paraphrase, model already loaded, texts cut into chunks of <=450 words
    "paraphrase_seconds_per_100_words": 8.0,
}
_EMA = 0.4  # weight of the newest run
_MIN_SWAPS = 5  # ignore tiny runs when calibrating
_MIN_WORDS = 50
CHUNK_WORDS_MAX = 900  # more than this per request only amortises model loading further


def chunk_words() -> int:
    """Words per request: the paraphrase step must fit text + rewrite into the context window."""
    try:
        num_ctx = int(os.environ.get("WATERMARKS_REWRITE_NUM_CTX", "") or 4096)
    except ValueError:
        num_ctx = 4096
    return max(150, min(CHUNK_WORDS_MAX, int((num_ctx - 600) / 7.5)))  # 4096 -> 466 words
MAX_PATCH_EDITS = 500

# tactics whose cost grows with the whole text (one long generation) rather than per word
_WHOLE_TEXT = {"paraphrase", "humanize", "structural", "backtranslate", "chunk", "code"}


def state_path() -> Path:
    env = os.environ.get("WATERMARKS_STATE_FILE", "").strip()
    return Path(env) if env else Path(tempfile.gettempdir()) / "watermarks-remover-estimate.json"


def _load_state() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _ema(entry: dict[str, Any] | None, value: float) -> dict[str, Any]:
    if not entry or entry.get("n", 0) < 1:
        return {"value": value, "n": 1}
    return {"value": (1 - _EMA) * entry["value"] + _EMA * value, "n": entry["n"] + 1}


def record_run(model: str, verify: bool, steps: list[dict[str, Any]]) -> None:
    """Fold the timing of a finished Layer B run into the calibration file (best effort)."""
    try:
        with _LOCK:
            state = _load_state()
            for step in steps:
                seconds = step.get("seconds")
                if not isinstance(seconds, (int, float)) or seconds <= 0:
                    continue
                if step.get("tactic") == "swap" and isinstance(step.get("swap"), dict):
                    swaps = step["swap"].get("accepted", 0)
                    if swaps >= _MIN_SWAPS:
                        key = f"swap|{model}|{'verify' if verify else 'plain'}"
                        state[key] = _ema(state.get(key), seconds / swaps)
                elif step.get("tactic") in _WHOLE_TEXT:
                    words = step.get("words", 0)
                    if words >= _MIN_WORDS:
                        key = f"whole|{model}|{step['tactic']}"
                        state[key] = _ema(state.get(key), seconds / (words / 100.0))
            state_path().write_text(json.dumps(state), encoding="utf-8")
    except OSError as e:
        # calibration is a convenience, never a reason to fail a request; but do say why
        sys.stderr.write(f"estimate: could not save calibration to {state_path()}: {e}\n")


# Layer B modes offered to the user: (id, title, strategy, meaning check, what it does).
# `options` of a mode is exactly what a client passes to /clean to run it.
MODES: list[tuple[str, str, str, bool | None, str]] = [
    ("paraphrase", "Только перефразирование", "paraphrase@0.8", None,
     "Модель переписывает текст целиком (перестраивает предложения). Быстрее всего; слов по одному не заменяет."),
    ("paraphrase_swap", "Перефразирование + замена слов (с проверкой смысла)", "paraphrase@0.8,swap@0.2", True,
     "После перефразирования ещё ~20% слов заменяются синонимами, и каждая замена проверяется. "
     "Сильнее всего меняет текст, качество замен лучшее."),
    ("paraphrase_swap_fast", "Перефразирование + замена слов (без проверки смысла)", "paraphrase@0.8,swap@0.2", False,
     "То же, но замены не проверяются: быстрее, зато больше неудачных синонимов (около 58% годных вместо 74%)."),
    ("swap_only", "Только замена слов (с проверкой смысла)", "swap@0.2", True,
     "Текст не перестраивается, меняется ~20% слов. Самое бережное к исходной формулировке."),
]


def build_modes(
    text: str,
    *,
    model: str,
    plan: dict[str, int],
    check_available: bool,
    current: tuple[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """Price every Layer B mode for *text* so a client can offer them as buttons.

    `check_available` is False when the meaning check cannot run (it needs Ollama); those modes
    are returned with ``available: false``. `current` is (strategy, meaning_check) of the
    service's configuration and marks the matching mode.
    """
    from rewrite_text import parse_strategy  # local import: avoids a hard import cycle at load

    out: list[dict[str, Any]] = []
    for mode_id, title, strategy, check, note in MODES:
        est = estimate_text(
            text, parse_strategy(strategy), model=model, verify=bool(check), plan=plan,
            with_alternatives=False,
        )
        options: dict[str, Any] = {"strategy": strategy}
        if check is not None:
            options["meaning_check"] = check
        available = check_available or not check
        out.append({
            "id": mode_id,
            "title": title,
            "note": note,
            "options": options,
            "available": available,
            "reason": None if available else "проверка смысла работает только с Ollama",
            "expected_seconds": est["expected_seconds"],
            "expected_range_seconds": est["expected_range_seconds"],
            "is_current": current is not None
            and current[0] == strategy and (check is None or current[1] == check),
        })
    fastest = min((m["expected_seconds"] for m in out if m["available"]), default=0) or 1
    for m in out:
        m["relative_time"] = round(m["expected_seconds"] / fastest, 1)
    return out


_NUM = re.compile(r"\d[\d.,]*\d|\d")
_SENT = re.compile(r"[.!?…]+(?:\s|$)")


def text_guards(before: str, after: str) -> dict[str, Any]:
    """Cheap, deterministic checks that a rewrite kept the facts it can be checked on.

    Numbers (years, quantities) that disappeared or appeared, how many sentences there were
    before and after (a dropped sentence shows up here), and the share of Cyrillic letters.
    """
    n_before, n_after = set(_NUM.findall(before)), set(_NUM.findall(after))

    def cyr(s: str) -> float:
        letters = [c for c in s if c.isalpha()]
        return round(sum("Ѐ" <= c <= "ӿ" for c in letters) / max(1, len(letters)), 3)

    return {
        "numbers_lost": sorted(n_before - n_after),
        "numbers_new": sorted(n_after - n_before),
        "sentences": [len(_SENT.findall(before)), len(_SENT.findall(after))],
        "words": [len(before.split()), len(after.split())],
        "cyrillic_share": [cyr(before), cyr(after)],
    }


def _profiles() -> list[dict[str, Any]]:
    env = os.environ.get("WATERMARKS_MODEL_PROFILES_FILE", "").strip()
    path = Path(env) if env else Path(__file__).resolve().parents[2] / "config" / "model_profiles.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        profiles = data.get("profiles", [])
        return [p for p in profiles if isinstance(p, dict) and p.get("model")]
    except (OSError, ValueError):
        return []


def installed_models(base_url: str | None) -> set[str] | None:
    """Names of the models Ollama has (None when it cannot be asked)."""
    if not base_url:
        return None
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/api/tags")
        with urllib.request.urlopen(req, timeout=2) as resp:  # noqa: S310 - the configured local endpoint
            data = json.loads(resp.read().decode("utf-8"))
        return {m.get("name", "") for m in data.get("models", [])}
    except Exception:  # noqa: BLE001 - unreachable Ollama just means "unknown"
        return None


def estimate_text(
    text: str,
    steps: list[tuple[str, float]],
    *,
    model: str,
    verify: bool,
    plan: dict[str, int],
    base_url: str | None = None,
    with_alternatives: bool = True,
) -> dict[str, Any]:
    """Expected duration of Layer B on *text*.

    `plan` is the swap plan (`words`, `candidates`, `target`) computed by rewrite_text.
    """
    state = _load_state()
    words = len(text.split())

    def compute(with_check: bool) -> tuple[list[dict[str, Any]], float, int, int]:
        detail: list[dict[str, Any]] = []
        samples = llm_steps = 0
        work = 0.0
        for tactic, level in steps:
            if tactic == "mlm":
                detail.append({"tactic": tactic, "seconds": None, "note": "local masked-LM step, not estimated"})
                continue
            llm_steps += 1
            if tactic == "swap":
                key = f"swap|{model}|{'verify' if with_check else 'plain'}"
                per = state.get(key, {}).get("value") or (
                    DEFAULTS["swap_seconds_per_swap_verify" if with_check else "swap_seconds_per_swap_plain"]
                )
                samples += state.get(key, {}).get("n", 0)
                swaps = max(1, round(level * plan["candidates"])) if plan["candidates"] else 0
                sec = swaps * per
                detail.append({"tactic": tactic, "target_swaps": swaps, "seconds_per_swap": round(per, 2),
                               "seconds": round(sec, 1)})
            else:
                key = f"whole|{model}|{tactic}"
                per100 = state.get(key, {}).get("value") or DEFAULTS["paraphrase_seconds_per_100_words"]
                samples += state.get(key, {}).get("n", 0)
                sec = words / 100.0 * per100
                detail.append({"tactic": tactic, "seconds_per_100_words": round(per100, 2),
                               "seconds": round(sec, 1)})
            work += sec
        return detail, work, samples, llm_steps

    detail, work_seconds, samples, llm_steps = compute(verify)
    other = compute(not verify)
    load = DEFAULTS["model_load_seconds"]
    total = work_seconds + llm_steps * load
    other_total = other[1] + other[3] * load
    measured = samples >= 3
    low, high = (0.85, 1.3) if measured else (0.7, 1.6)

    per_chunk = chunk_words()
    chunks = max(1, math.ceil(words / per_chunk))
    # every request loads (and unloads) the model once per LLM step, so chunking adds loads
    chunked_total = work_seconds + chunks * llm_steps * load
    return {
        "words": words,
        "model": model,
        "meaning_check": verify,
        "expected_seconds_with_meaning_check": round(total if verify else other_total),
        "expected_seconds_without_meaning_check": round(other_total if verify else total),
        "steps": detail,
        "model_load_seconds": load,
        "expected_seconds": round(total),
        "expected_range_seconds": [round(total * low), round(total * high)],
        "basis": "measured on this machine" if measured else "default constants (not yet calibrated here)",
        "calibration_samples": samples,
        "chunk_plan": {
            "chunk_words": per_chunk,
            "chunks": chunks,
            "expected_seconds_if_chunked": round(chunked_total),
            # with options.keep_model the model is loaded once for all chunks (and freed by /unload)
            "expected_seconds_if_chunked_keep_model": round(total),
            "note": "a long text should be sent in chunks: progress is visible and a stop is possible between chunks",
        },
        "alternatives": _alternatives(total, detail, model, verify, base_url) if with_alternatives else [],
    }


def _factor(profile: dict[str, Any], tactic: str) -> float:
    """Relative time of one Layer B tactic for a profile (the main model = 1)."""
    swap = float(profile.get("time_factor", 1.0))
    if tactic == "swap":
        return swap
    return float(profile.get("paraphrase_factor", swap))


def _alternatives(
    total: float, detail: list[dict[str, Any]], model: str, verify: bool, base_url: str | None
) -> list[dict[str, Any]]:
    """Other models with their relative speed and a quality note (see config/model_profiles.json).

    Each tactic is scaled by its own factor (paraphrasing and word swapping slow down differently).
    """
    profiles = _profiles()
    if not profiles:
        return []
    have = installed_models(base_url)
    base = next((p for p in profiles if p["model"] == model), {})
    secs = [(d.get("tactic"), float(d.get("seconds") or 0.0)) for d in detail]
    work = sum(s for _, s in secs) or 1.0
    out = []
    for p in profiles:
        if p["model"] == model:
            continue
        scaled = sum(s * _factor(p, t) / _factor(base, t) for t, s in secs)
        factor = scaled / work
        out.append({
            "model": p["model"],
            "title": p.get("title", p["model"]),
            "installed": None if have is None else p["model"] in have,
            "expected_seconds": round(total * factor),
            "relative_time": round(factor, 2),
            "quality": p.get("quality", ""),
            "meaning_check_advised": bool(p.get("meaning_check_advised", True)),
            "verify_min": p.get("verify_min"),
        })
    out.sort(key=lambda a: a["expected_seconds"])
    return out


def apply_patch(text: str, edits: Any) -> tuple[str, list[int], list[dict[str, Any]]]:
    """Replace exactly the listed whole words; return (new text, applied indexes, skipped).

    Each edit is ``{"offset": int, "old": str, "new": str}`` and must match the current text:
    ``text[offset:offset+len(old)] == old`` and ``old`` must be a whole word. Edits are applied
    right to left so offsets stay valid. Nothing outside the listed words changes.
    """
    if not isinstance(edits, list):
        raise ValueError("'edits' must be a list")
    if len(edits) > MAX_PATCH_EDITS:
        raise ValueError(f"too many edits (max {MAX_PATCH_EDITS})")
    parsed: list[tuple[int, int, str, str]] = []
    skipped: list[dict[str, Any]] = []
    for idx, e in enumerate(edits):
        if not (isinstance(e, dict) and isinstance(e.get("offset"), int)
                and isinstance(e.get("old"), str) and isinstance(e.get("new"), str)):
            raise ValueError(f"edit {idx}: needs integer 'offset' and string 'old', 'new'")
        old, new = e["old"], e["new"]
        if not old or not new or len(new) > 60 or any(c in new for c in "\r\n\t"):
            skipped.append({"index": idx, "reason": "empty or unsuitable replacement"})
            continue
        parsed.append((idx, e["offset"], old, new))
    applied: list[int] = []
    boundary = len(text) + 1  # start of the previously applied (right-hand) edit
    for idx, off, old, new in sorted(parsed, key=lambda p: p[1], reverse=True):
        end = off + len(old)
        if off < 0 or end > len(text) or text[off:end] != old:
            skipped.append({"index": idx, "reason": "the text at this offset differs from 'old'"})
        elif (off > 0 and text[off - 1].isalnum()) or (end < len(text) and text[end].isalnum()):
            skipped.append({"index": idx, "reason": "'old' is not a whole word"})
        elif end > boundary:
            skipped.append({"index": idx, "reason": "overlaps another edit"})
        else:
            text = text[:off] + new + text[end:]
            boundary = off
            applied.append(idx)
    return text, sorted(applied), sorted(skipped, key=lambda s: s["index"])
