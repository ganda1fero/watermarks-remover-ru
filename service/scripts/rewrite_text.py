#!/usr/bin/env python3
"""Layer B optional rewrite hook for statistical (token-sampling) watermarks.

Backends:
  print-prompt       — emit prompt only (default; CI-safe, no model)
  ollama             — POST to Ollama /api/chat
  openai-compatible  — POST to OpenAI-style /v1/chat/completions

Env (optional):
  WATERMARKS_REWRITE_BACKEND
  WATERMARKS_REWRITE_BASE_URL
  WATERMARKS_REWRITE_MODEL
  WATERMARKS_REWRITE_API_KEY      (env-only; never pass keys on argv)
  WATERMARKS_REWRITE_ALLOW_REMOTE (set to 1 to allow non-loopback endpoints)
  WATERMARKS_REWRITE_CANDIDATES   (default 1; variants generated per loop)
  WATERMARKS_REWRITE_LOOPS        (default 1; max evaluation rounds)

Rewriting is iterative and evaluation-driven: each loop generates
--candidates (default 1) variants, evaluates each, and stops as soon as an
attempt passes watermark detection; --max-loops (default 1) caps how many
evaluation rounds run before the best-effort variant is returned
(WATERMARKS_REWRITE_LOOPS). The evaluator is chosen by priority: keyed-Gumbel
same-key replay (when --gumbel-key / WATERMARKS_GUMBEL_KEY is set), else
MarkLLM same-config detection (--markllm-scheme), else, when no detector is
configured, bigram-Jaccard lexical divergence (no pass/fail verdict — all
attempts are generated and the most diverged one is selected). A vendor-detector
seam (Google's retired SynthID-text detector) is reserved ahead of the
same-config detectors should a vendor endpoint return.

The rewrite instruction comes from --tactic (a named prompt) and, when
--rewrite-level is set, that prompt is further modulated by a numeric rewrite
intensity in (0,1] that controls how many tokens change (0 — the unchanged
original — is excluded; 1 rewrites everything). The level is a request: output
lexical/semantic divergence is measured, not guaranteed. --style appends an
optional writing-style instruction (e.g. "write like Hemingway"), most useful
with --tactic humanize; it is a request, not a guarantee, and never overrides
the fact/voice rules. The humanize tactic additionally runs a deterministic
humanizer pass (humanize_pass.py) over each generated candidate — straight
quotes, no em/en dashes or double hyphens, filler-phrase collapses, and the
utilize->use swap — before evaluation, so the scored text is the text returned.

Security notes:
  - Only http(s) endpoints are accepted; redirects are refused outright so an
    Authorization header (API key) can never be re-sent to an unvalidated host.
  - Non-loopback endpoints are denied unless WATERMARKS_REWRITE_ALLOW_REMOTE=1
    (or --allow-remote) is set explicitly.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import cleaned_path, eprint, read_text_input, write_text_output
from humanize_pass import humanize_pass
from text_detectors import GumbelTextDetector, MarkLLMTextDetector
from text_unicode import clean_text

DEFAULT_MARKLLM_MODEL = "facebook/opt-1.3b"
DEFAULT_CANDIDATES = 1
DEFAULT_MAX_LOOPS = 1

PROMPTS = {
    "paraphrase": (
        "Rewrite the following text so that it uses substantially different wording at "
        "the token level. Change clause order, connectors, and transition words; vary "
        "sentence boundaries and length; and replace both content words and function "
        "words where meaning allows. Preserve all facts, numbers, names, and technical "
        "identifiers. Do not add or remove claims. Output only the rewritten text.\n\n---\n{TEXT}"
    ),
    "humanize": (
        "Rewrite the following text so it reads as if a human wrote it from scratch. "
        "Vary sentence rhythm and length unevenly — mix short and long sentences instead "
        "of a steady mid-length cadence — and merge or split paragraphs where a human "
        "would. Use plain, concrete wording and simple verbs (is/are/has); prefer active "
        'voice. Cut promotional language and inflated significance ("stands as a '
        'testament", "pivotal", "vibrant", "a rich tapestry"), superficial '
        'present-participle analyses ("reflecting", "showcasing", "underscoring"), '
        'vague attributions ("experts argue"), rule-of-three listing, filler ("in order '
        'to", "it is important to note"), hedging, and formulaic positive conclusions. '
        'Avoid AI vocabulary ("additionally", "delve", "crucial", "foster", '
        '"leverage", "utilize", "interplay", and abstract "landscape"). Do not add '
        "em dashes, bold text, emojis, or curly quotes. Preserve all facts, numbers, "
        "names, and technical identifiers. Do not add or remove claims. Output only the "
        "rewritten text.\n\n---\n{TEXT}"
    ),
    "code": (
        "Rewrite the natural-language parts of this code — comments, docstrings, and "
        "string literals — using different wording. Rename local variables, function "
        "parameters, and private helper names to semantically equivalent names. Preserve "
        "program behavior, public API names, and all values that affect output. Output "
        "only the rewritten code.\n\n---\n{TEXT}"
    ),
    "backtranslate_out": (
        "Translate the following text to {LANG}. Output only the translation.\n\n---\n{TEXT}"
    ),
    "backtranslate_back": (
        "Translate the following text to {ORIGINAL_LANG}. Preserve meaning; use natural "
        "phrasing. Output only the translation.\n\n---\n{TEXT}"
    ),
    "structural_outline": (
        "Extract a bullet outline of all claims and structure from the text "
        "(no full sentences). Output only the outline.\n\n---\n{TEXT}"
    ),
    "structural_write": (
        "Write a complete document from this outline in natural, varied human prose. "
        "Avoid formulaic transitions. Do not omit any bullet. Output only the document."
        "\n\n---\n{TEXT}"
    ),
    # LLM stand-in for the local masked-LM edit (`mlm`): works in any language,
    # unlike roberta-large (English only).
    "swap": (
        "In the following text, replace a portion of the content words (nouns, verbs, "
        "adjectives, adverbs) with fitting synonyms or close alternatives that keep the "
        "meaning and grammar correct. Leave function words, sentence structure, "
        "numbers, names, and technical identifiers unchanged. Output only the edited "
        "text.\n\n---\n{TEXT}"
    ),
    "level": (
        "Rewrite the following text so that a fraction of the tokens close to "
        "{LEVEL:.2f} changes — 0 would mean the wording is kept unchanged, 1 means "
        "everything is rewritten. At low values keep the sentence structure, word "
        "order, and every token that can stay, changing only function words and a "
        "few non-essential content words. At high values change wording substantially "
        "at the token level. Preserve all facts, numbers, names, and technical "
        "identifiers. Do not add or remove claims. Output only the rewritten text."
        "\n\n---\n{TEXT}"
    ),
    "chunk_unit": (
        "Rewrite only this fragment to change a modest fraction of its tokens. "
        "At low intensity keep the sentence structure, word order, and every "
        "token that can stay, changing only function words and a few "
        "non-essential content words. Preserve all facts, numbers, names, and "
        "technical identifiers. Do not add or remove claims. Output only the "
        "rewritten fragment.\n\n---\n{TEXT}"
    ),
}


def _tokens(text: str) -> list[str]:
    """Extract lowercase alphanumeric/word tokens from text."""
    return re.findall(r"\w+", text.lower())


def _bigrams(tokens: list[str]) -> set[tuple[str, str]]:
    """Extract consecutive token pairs as bigrams."""
    return set(itertools.pairwise(tokens))


def _lexical_divergence(original: str, candidate: str) -> float:
    """Bigram Jaccard distance: 0.0 identical, 1.0 fully different."""
    a = _tokens(original)
    b = _tokens(candidate)
    if not a and not b:
        return 0.0
    if not a or not b:
        return 1.0
    ba = _bigrams(a)
    bb = _bigrams(b)
    union = ba | bb
    if not union:
        return 0.0
    return 1.0 - len(ba & bb) / len(union)


def _select_candidate(original: str, candidates: list[str]) -> tuple[str, list[float]]:
    """Pick the most lexically diverged rewrite, gently guarding extreme length drift."""
    scores: list[float] = []
    for cand in candidates:
        score = _lexical_divergence(original, cand)
        if original:
            ratio = len(cand) / len(original)
            if ratio > 2.0 or ratio < 0.5:
                score -= 0.15
        scores.append(score)
    best_idx = max(range(len(candidates)), key=lambda i: scores[i])
    return candidates[best_idx], scores


def _env(name: str, default: str | None = None) -> str | None:
    """Read an environment variable with fallback."""
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v


def _flag_env(name: str) -> bool:
    """Read an environment variable as a boolean flag."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    """Read an environment variable as an integer."""
    try:
        return int(_env(name, str(default)) or str(default))
    except ValueError:
        return default


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _check_remote(base_url: str, allow_remote: bool) -> None:
    """Enforce the rewrite-endpoint allowlist.

    Default-deny: only loopback endpoints are accepted. Anything else requires
    an explicit opt-in (--allow-remote / WATERMARKS_REWRITE_ALLOW_REMOTE=1),
    and non-http(s) schemes (e.g. file://) are always refused.
    """
    u = urlparse(base_url)
    if u.scheme not in ("http", "https"):
        raise SystemExit(
            f"error: rewrite base URL must be http(s), got scheme '{u.scheme}': {base_url}"
        )
    host = u.hostname or ""
    if host in _LOOPBACK_HOSTS:
        return
    if not allow_remote:
        raise SystemExit(
            "error: rewrite base URL host is not loopback "
            f"('{host}'); refusing to send content off-machine. "
            "Set WATERMARKS_REWRITE_ALLOW_REMOTE=1 or pass --allow-remote to override."
        )
    eprint(
        f"warning: rewrite base URL host is '{host}' (not localhost); "
        "content will leave this machine"
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse HTTP redirects.

    urllib's default handler re-sends the request headers on 301/302/303,
    which would forward the Authorization header (API key) to an unvalidated
    host behind the localhost allowlist. Any 3xx now surfaces as HTTPError.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Custom redirect handler for URL requests."""
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def _safe_detect(detector: object, text: str) -> dict:
    """Run a detector report defensively; a raising detector never fails the rewrite."""
    try:
        return detector.detect(text)  # type: ignore[attr-defined]
    except Exception as e:  # defensive: the detector contract is fail-soft
        return {"available": False, "error": f"evaluation failed: {e}"}


def _pick_evaluator(
    markllm_detector: MarkLLMTextDetector | None,
    gumbel_detector: GumbelTextDetector | None,
) -> tuple[str, object | None]:
    """Pick the evaluator that drives the iterative rewrite loop.

    Priority: keyed-Gumbel same-key replay (when the caller passed
    --gumbel-key) > MarkLLM same-config detection (--markllm-scheme) >
    bigram-Jaccard lexical divergence (fallback with no pass/fail verdict).
    A vendor-detector seam (Google's SynthID-text detector, retired Aug 2026)
    is reserved ahead of both should a vendor endpoint return; it only needs
    available()/detect()/name per the TextDetector protocol in
    text_detectors.py.
    """
    if gumbel_detector is not None:
        return "gumbel", gumbel_detector
    if markllm_detector is not None:
        return "markllm", markllm_detector
    return "lexical-divergence", None


def _generate_once(
    backend: str,
    base_url: str,
    model: str,
    api_key: str | None,
    prompt: str,
    timeout: float,
    temperature: float,
    reasoning_effort: str | None,
    keep_alive: str | int | None = None,
    max_tokens: int | None = None,
) -> str:
    """Generate a single rewrite variant through the configured backend."""
    if backend == "ollama":
        extra: dict[str, Any] = {}
        if keep_alive is not None:
            extra["keep_alive"] = keep_alive
        if max_tokens is not None:
            extra["max_tokens"] = max_tokens
        return call_ollama(base_url, model, prompt, timeout, temperature, **extra)
    if backend == "openai-compatible":
        return call_openai_compatible(
            base_url, model, prompt, api_key, timeout, temperature, reasoning_effort
        )
    raise SystemExit(f"unknown backend: {backend}")


def _tactic_prompt(tactic: str, text: str, lang: str, original_lang: str) -> str:
    """Build the prompt for a named rewrite tactic (no intensity modulation)."""
    if tactic == "paraphrase":
        return PROMPTS["paraphrase"].format(TEXT=text)
    if tactic == "humanize":
        return PROMPTS["humanize"].format(TEXT=text)
    if tactic == "code":
        return PROMPTS["code"].format(TEXT=text)
    if tactic == "backtranslate":
        # single combined instruction for print-prompt / one-shot backends
        return (
            f"Translate the text to {lang}, then translate that result back to "
            f"{original_lang}. Preserve all facts, numbers, and names. "
            f"Output only the final {original_lang} text.\n\n---\n{text}"
        )
    if tactic == "structural":
        return (
            "First extract a bullet outline of all claims (no full sentences). "
            "Then write a complete document from that outline in natural, varied human "
            "prose without omitting any bullet. Output only the final document.\n\n---\n"
            f"{text}"
        )
    if tactic == "chunk":
        return PROMPTS["chunk_unit"].format(TEXT=text)
    if tactic == "swap":
        return PROMPTS["swap"].format(TEXT=text)
    if tactic == "mlm":
        # Local masked-LM edit: the prompt is informational only; generation runs
        # a non-autoregressive infill (see _mlm_infill) rather than the backend.
        return "Local masked-LM infill; no LLM prompt is used.\n\n---\n" + text
    raise ValueError(f"unknown tactic: {tactic}")


def _intensity_clause(level: float) -> str:
    """The intensity instruction appended to a tactic prompt.

    The level is a request, not a contract: measured lexical/semantic
    divergence is the real outcome, and a model may not hit the fraction exactly.
    """
    return (
        f"Modulate this rewrite so roughly a fraction {level:.2f} of tokens change: "
        "0 would keep the wording unchanged, 1 rewrites everything. At low intensity "
        "keep the sentence structure, word order, and every token that can stay, "
        "changing only function words and a few non-essential content words; at high "
        "intensity change wording substantially at the token level. Preserve all facts, "
        "numbers, names, and technical identifiers. Do not add or remove claims."
    )


# Function words / short / technical tokens we never hand to a masked LM.
_MLM_SKIP_WORDS = {
    "the",
    "and",
    "for",
    "are",
    "but",
    "not",
    "you",
    "all",
    "can",
    "had",
    "her",
    "was",
    "one",
    "our",
    "out",
    "day",
    "get",
    "has",
    "him",
    "his",
    "how",
    "man",
    "new",
    "now",
    "old",
    "see",
    "two",
    "way",
    "who",
    "boy",
    "did",
    "its",
    "let",
    "put",
    "say",
    "she",
    "too",
    "use",
    "that",
    "with",
    "have",
    "this",
    "will",
    "your",
    "from",
    "they",
    "been",
    "were",
    "would",
    "there",
    "their",
    "what",
    "when",
    "which",
    "also",
    "into",
    "than",
    "then",
    "them",
    "these",
    "those",
    "such",
    "only",
    "very",
    "just",
    "about",
    "some",
    "more",
    "most",
    "other",
    "over",
    "under",
    "through",
    "between",
    "while",
    "where",
    "because",
}
# Russian function words longer than 3 letters (shorter ones are already skipped).
_MLM_SKIP_WORDS |= {
    "этот", "этого", "этому", "этом", "этой", "этим", "этих", "эта", "это", "эти", "того",
    "тому", "тем", "чем", "чтобы", "который", "которая", "которое", "которые", "которых",
    "которым", "которого", "которой", "котором", "когда", "если", "хотя", "пока", "также",
    "только", "даже", "очень", "более", "менее", "всего", "всех", "всем", "весь", "вся",
    "всё", "свой", "своей", "своих", "своего", "свои", "своим", "себя", "него", "неё",
    "них", "нами", "нас", "вас", "вам", "ним", "ней", "есть", "было", "были", "была",
    "будет", "быть", "может", "могут", "можно", "нужно", "надо", "между", "через", "перед",
    "после", "около", "среди", "потому", "поэтому", "затем", "тогда", "здесь", "там",
    "тут", "ещё", "уже", "либо", "или", "тоже", "однако", "ведь", "именно", "лишь",
    "будто", "словно", "почти", "вместе", "вместо", "кроме", "против", "ради", "чтоб",
    "нибудь", "кто-то", "какой", "какая", "какое", "какие", "каждый", "каждая", "каждое",
    "каждые", "другой", "другая", "другое", "другие", "сам", "сама", "само", "сами",
    "один", "одна", "одно", "одни", "много", "мало", "очень", "тоже", "ещё",
}
_MLM_TOKEN_RE = re.compile(r"(\s+|[.,;:!?()\"'—-])")
_MLM_MAX_TOKENS = 512  # roberta-large positional limit
_MLM_CACHE: dict[str, Any] = {}  # {"pipeline": ..., "mask_token": ...}


def _cuda_available() -> bool:
    """True when a CUDA device is usable; False on CPU-only or no-torch hosts."""
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:  # torch absent; run on CPU/auto
        return False


def _get_mlm() -> tuple[Any, str]:
    """Return the process-cached roberta-large fill-mask pipeline + mask token.

    Built lazily on first use; a failed import surfaces as RuntimeError (fail-soft
    optional dependency). The device is chosen at runtime so a CPU-only host still
    works (architecture selects the accelerator when available).
    """
    if "pipeline" not in _MLM_CACHE:
        try:
            from transformers import pipeline
        except Exception as e:  # fail-soft: optional dependency
            raise RuntimeError(f"mlm tactic unavailable: {e}") from e
        # roberta-large is English-only; set WATERMARKS_MLM_MODEL to a multilingual
        # fill-mask model (e.g. xlm-roberta-base) for other languages.
        kwargs: dict[str, Any] = {"model": _env("WATERMARKS_MLM_MODEL", "roberta-large")}
        if _cuda_available():
            kwargs["device"] = 0
        _MLM_CACHE["pipeline"] = pipeline("fill-mask", **kwargs)
        _MLM_CACHE["mask_token"] = _MLM_CACHE["pipeline"].tokenizer.mask_token
    return _MLM_CACHE["pipeline"], _MLM_CACHE["mask_token"]


def _mlm_chunks(parts: list[str], tokenizer: Any, max_tokens: int = _MLM_MAX_TOKENS):
    """Split `parts` into contiguous chunks whose token length stays <= max_tokens.

    Chunk boundaries fall on separator/word edges, so each chunk joins to a clean
    substring, preserving ordering across chunks. Yields (global_start, chunk).
    """
    cur: list[str] = []
    cur_start = 0
    for i, part in enumerate(parts):
        trial = [*cur, part]
        if cur and len(tokenizer("".join(trial))["input_ids"]) > max_tokens:
            yield cur_start, cur
            cur = [part]
            cur_start = i
        else:
            cur = trial
    if cur:
        yield cur_start, cur


def _mlm_infill(text: str, level: float) -> str:
    """Mask `level` of content words and infill with roberta-large (local edit).

    Non-autoregressive: the output is a mix of the original token stream and
    masked-LM predictions, not fresh LLM-sampled prose. Uses a process-cached,
    runtime-device pipeline and splits inputs longer than roberta's positional
    limit into separately-infilled chunks.
    """
    mlm, mask_token = _get_mlm()
    tokenizer = mlm.tokenizer
    tokens = _MLM_TOKEN_RE.split(text)
    orig = list(tokens)
    content = [
        i
        for i, t in enumerate(tokens)
        if t.strip()
        and t.isalpha()
        and len(t) > 3
        and t.lower() not in _MLM_SKIP_WORDS
        and not t[0].isupper()
    ]
    k = max(1, round(level * len(content))) if content else 0
    if k == 0:
        return text
    step = len(content) / k
    mset: set[int] = set()
    pos = 0.0
    for _ in range(k):
        idx = content[int(pos)]
        mset.add(idx)
        pos += step
    out_parts = [mask_token if i in mset else tokens[i] for i in range(len(tokens))]
    for chunk_start, chunk in _mlm_chunks(out_parts, tokenizer):
        positions = [chunk_start + j for j, p in enumerate(chunk) if p == mask_token]
        if not positions:
            continue
        preds = mlm("".join(chunk), top_k=1)
        picks = [(p if isinstance(p, dict) else p[0]) for p in preds]
        for k_i, global_idx in enumerate(positions):
            if k_i < len(picks):
                guess = picks[k_i]["token_str"].strip()
                # A subword model may predict a fragment or punctuation; keep the
                # original word then instead of corrupting it.
                tokens[global_idx] = guess if guess.isalpha() and len(guess) > 1 else orig[global_idx]
    return "".join(tokens)


# --- swap: LLM synonym substitution of an algorithmically chosen word quota ------
# The code decides *which* words change (exact, reproducible quota); the model only
# answers, per word, with one same-form synonym or NULL. Every word is its own
# request carrying just the words around it (no shared, cached block), so the
# model's attention stays on that word. Refused/invalid words are remembered and a
# fresh word is drawn instead until the quota is met. For Russian, replacements are
# also checked against a dictionary and for grammatical-form agreement (pymorphy3).
_SWAP_SKIP_STEMS = (
    "процент", "рубл", "доллар", "евро", "километр", "сантиметр", "миллиметр",
    "килограмм", "миллиард", "миллион", "тысяч", "градус", "секунд", "минут",
    # number words (a swap would change the quantity)
    "двух", "трёх", "трех", "четырёх", "четырех", "пяти", "шести", "семи", "восьми",
    "девяти", "десяти", "двадцат", "тридцат", "сорока", "пятидесят", "одиннадцат",
    "двенадцат", "тринадцат", "четырнадцат", "пятнадцат", "шестнадцат", "семнадцат",
    "восемнадцат", "девятнадцат", "сотен", "десятк", "пятьсот",
    # months (a swap would change the date)
    "январ", "феврал", "март", "апрел", "июн", "июл", "август", "сентябр", "октябр",
    "ноябр", "декабр",
)
# Date/period words are left alone as well: "1957 года" -> "лета" changes a fact.
_SWAP_SKIP_WORDS = frozenset({
    "май", "мая", "мае", "маю", "год", "года", "году", "годом", "годе", "годы", "годов",
    "годам", "лет", "век", "века", "веке", "веком", "веков",
})
_SWAP_NUMBER_ZONE = 2  # words within this distance of a digit-bearing word are untouched
_SWAP_MAX_ROUNDS = 6
_SWAP_CHARS_PER_TOKEN = 2.5  # conservative size estimate for the context budget

# {window} = the words around the target with the target marked [[word]]; {word} = target.
_SWAP_PROMPTS = {
    "ru": (
        "Замени слово синонимом.\n\n"
        "Фрагмент: {window}\n\n"
        "Слово: «{word}» (в фрагменте выделено [[ ]]).\n\n"
        "Нужен один синоним, подходящий по смыслу и стоящий в точно такой же "
        "грамматической форме (падеж, число, род, время), чтобы предложение осталось "
        "верным и грамотным. Только существующие русские слова. Если синонима нет или "
        "ты не уверен в замене, ответь NULL. Числа, единицы измерения, имена и термины "
        "не заменяй.\n"
        "Ответ: одно слово или NULL."
    ),
    "ru_short": (
        "Фрагмент: {window}\n"
        "Найди синоним к слову «{word}» (выделено [[ ]]) в той же форме. "
        "Не уверен — NULL.\n"
        "Ответ: одно слово."
    ),
    "en": (
        "Replace the word with a synonym.\n\n"
        "Fragment: {window}\n\n"
        'Word: "{word}" (marked [[ ]] in the fragment).\n\n'
        "Give one synonym that fits the meaning and has exactly the same grammatical "
        "form (case, number, gender, tense), so the sentence stays correct. Use only "
        "real words of the same language. If there is no synonym or you are not sure, "
        "answer NULL. Do not replace numbers, units of measure, names or terms.\n"
        "Answer: one word or NULL."
    ),
}


def _swap_candidate(tok: str) -> bool:
    low = tok.lower()
    return (
        tok.isalpha()
        and len(tok) > 3
        and not tok[0].isupper()
        and low not in _MLM_SKIP_WORDS
        and low not in _SWAP_SKIP_WORDS
        and not low.startswith(_SWAP_SKIP_STEMS)
    )


def _script_of(s: str) -> str:
    letters = [c for c in s if c.isalpha()]
    cyr = sum(1 for c in letters if "Ѐ" <= c <= "ӿ")
    return "cyrillic" if letters and cyr / len(letters) > 0.5 else "other"


def _valid_swap(orig: str, new: Any) -> str | None:
    """Accept a replacement only if it is a plain, different word in the same script."""
    if not isinstance(new, str):
        return None
    new = new.strip().strip("[]\"'«»")
    words = new.split()
    if not new or len(new) > 40 or len(words) > 2 or new.lower() == orig.lower():
        return None
    if not all(w.replace("-", "").isalpha() for w in words):
        return None
    # No script mixing: a Cyrillic word may be replaced only by all-Cyrillic text
    # (and a non-Cyrillic one only by non-Cyrillic), which drops hybrids like "сkeptиками".
    is_cyr = [("Ѐ" <= c <= "ӿ") for c in new if c.isalpha()]
    want_cyr = _script_of(orig) == "cyrillic"
    if want_cyr and not all(is_cyr):
        return None
    if not want_cyr and any(is_cyr):
        return None
    return new


# --- optional Russian morphology (pymorphy3): dictionary + grammatical-form check ---
_MORPH: Any = None  # MorphAnalyzer once loaded; False when pymorphy3 is unavailable
_CASE_NORM = {"gen2": "gent", "loc2": "loct", "acc2": "accs", "voct": "nomn"}
_POS_GROUP = {"ADJF": "ADJ", "PRTF": "ADJ", "ADJS": "ADJS", "PRTS": "ADJS"}


def _get_morph() -> Any:
    global _MORPH
    if _MORPH is None:
        try:
            import pymorphy3

            _MORPH = pymorphy3.MorphAnalyzer()
        except Exception:  # not installed / dictionaries missing
            _MORPH = False
    return _MORPH or None


def _forms(word: str) -> list[tuple[Any, ...]]:
    """Grammatical readings of `word` as (group, case, number, tense, person, mood, gender)."""
    morph = _get_morph()
    out = []
    for p in morph.parse(word.lower())[:6]:
        t = p.tag

        def g(x: Any) -> str | None:  # pymorphy grammemes -> plain strings
            return str(x) if x else None

        pos, case, tense = g(t.POS), g(t.case), g(t.tense)
        group = _POS_GROUP.get(pos, pos) if pos else None
        case = _CASE_NORM.get(case, case) if case else None
        # Gender is only an agreement feature for adjectives/participles and past verbs.
        agrees = group in ("ADJ", "ADJS") or (group == "VERB" and tense == "past")
        out.append((group, case, g(t.number), tense, g(t.person), g(t.mood),
                    g(t.gender) if agrees else None))
    return out


def _form_ok(orig: str, new: str) -> bool:
    """True when some reading of `new` agrees with some reading of `orig` in every
    grammatical feature both define (part of speech, case, number, tense, person, mood,
    and gender for agreeing forms). Synonyms may differ in aspect and in noun gender."""
    for a in _forms(orig):
        for b in _forms(new):
            if a[0] == b[0] and all(x is None or y is None or x == y for x, y in zip(a[1:], b[1:])):
                return True
    return False


def _spread(pool: list[int], k: int) -> list[int]:
    if k >= len(pool):
        return list(pool)
    step = len(pool) / k
    return [pool[int(i * step)] for i in range(k)]


def _swap_prompt(kind: str, window: str, word: str) -> str:
    return _SWAP_PROMPTS[kind].replace("{word}", word).replace("{window}", window)


def _parse_swap_reply(reply: str) -> str | None:
    """First line of the reply as a bare word; None for a refusal."""
    line = (reply.strip().splitlines() or [""])[0].strip().strip(".,;:!?\"'«»*`[]() ")
    if not line or line.upper() in ("NONE", "NULL", "НЕТ", "N/A"):
        return None
    return line


# --- meaning check: a second call scores whether the new word keeps the meaning -------
# Calibrated on labelled swaps (see experiments): comparing the two phrases and reading
# P("ДА") vs P("НЕТ") separates good from bad replacements far better than a plain yes/no
# (AUC ~0.85 with YandexGPT-5-Lite); the threshold is the trade-off knob.
_VERIFY_PROMPT = (
    "Исходная фраза: {left} {orig} {right}\n"
    "Новая фраза: {left} {new} {right}\n\n"
    "Значат ли эти две фразы одно и то же, и грамотна ли новая? "
    "Если есть сомнение, ответь НЕТ.\n"
    "Ответ: ДА или НЕТ."
)
_VERIFY_WORDS = 5  # words of context on each side shown to the checker


def _verify_prompt(left: str, orig: str, new: str, right: str) -> str:
    return (
        _VERIFY_PROMPT.replace("{left}", left).replace("{right}", right)
        .replace("{orig}", orig).replace("{new}", new)
    )


def _profile_for(model: str | None) -> dict[str, Any]:
    env = _env("WATERMARKS_MODEL_PROFILES_FILE")
    path = Path(env) if env else Path(__file__).resolve().parents[2] / "config" / "model_profiles.json"
    try:
        for p in json.loads(path.read_text(encoding="utf-8")).get("profiles", []):
            if p.get("model") == model:
                return p
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def _has_profile_threshold(model: str | None) -> bool:
    return _profile_for(model).get("verify_min") is not None


def _verify_min_for(model: str | None) -> float:
    """Meaning-check threshold: WATERMARKS_SWAP_VERIFY_MIN, else the model's profile, else 12."""
    explicit = _env("WATERMARKS_SWAP_VERIFY_MIN")
    if explicit:
        try:
            return float(explicit)
        except ValueError:
            pass
    profile = _profile_for(model)
    if profile.get("verify_min") is not None:
        return float(profile["verify_min"])
    return 12.0


def unload_model(base_url: str, model: str) -> bool:
    """Ask Ollama to free the model's memory now (keep_alive 0). Best effort."""
    try:
        _http_json(base_url.rstrip("/") + "/api/generate", {"model": model, "keep_alive": 0}, {}, 30)
        return True
    except Exception:  # noqa: BLE001 - Ollama's own timer unloads it anyway
        return False


def _verify_score(
    base_url: str, model: str, prompt: str, timeout: float, keep_alive: str | int | None
) -> float:
    """log P(yes) - log P(no) of the first answer token (Ollama logprobs)."""
    options: dict[str, Any] = {"temperature": 0, "num_predict": 6}
    num_ctx = _env_int("WATERMARKS_REWRITE_NUM_CTX", 0)
    if num_ctx > 0:
        options["num_ctx"] = num_ctx
    payload: dict[str, Any] = {
        "model": model, "stream": False, "options": options,
        "messages": [{"role": "user", "content": prompt}],
        "logprobs": True, "top_logprobs": 10,
    }
    if keep_alive is not None:
        payload["keep_alive"] = keep_alive
    data = _http_json(base_url.rstrip("/") + "/api/chat", payload, {}, timeout)
    for entry in data.get("logprobs") or []:
        if not str(entry.get("token", "")).strip():
            continue  # a leading space token
        yes = no = 0.0
        for t in entry.get("top_logprobs") or []:
            w = str(t.get("token", "")).strip().upper().replace("Ё", "Е").rstrip(".,:!")
            if w in ("ДА", "YES"):
                yes += math.exp(t["logprob"])
            elif w in ("НЕТ", "НЕ", "NO"):
                no += math.exp(t["logprob"])
        return math.log(yes + 1e-9) - math.log(no + 1e-9)
    # server without logprobs: fall back to the textual answer
    text = str((data.get("message") or {}).get("content") or "").strip().upper()
    return 30.0 if text.startswith(("ДА", "YES")) else -30.0


def _swap_plan(
    text: str, level: float
) -> tuple[list[str], list[int], dict[int, int], list[int], list[int], int]:
    """Tokenise *text* and choose the swap candidates.

    Returns (tokens, word_pos, pos_of, all_cand, cand, target): `word_pos` are the token
    indexes that are words, `all_cand` the content words that could be swapped, `cand` those
    left after dropping words near numbers, and `target` how many swaps `level` asks for.
    """
    tokens = _MLM_TOKEN_RE.split(text)
    word_pos = [i for i, t in enumerate(tokens) if any(c.isalnum() for c in t)]
    pos_of = {i: p for p, i in enumerate(word_pos)}
    # Words next to a number (dates, quantities, versions) are never swapped.
    numeric = {p for p, i in enumerate(word_pos) if any(c.isdigit() for c in tokens[i])}
    near_number = {
        q for p in numeric
        for q in range(p - _SWAP_NUMBER_ZONE, p + _SWAP_NUMBER_ZONE + 1)
    }
    all_cand = [i for i in word_pos if _swap_candidate(tokens[i])]
    cand = [i for i in all_cand if pos_of[i] not in near_number]
    target = min(len(cand), max(1, round(level * len(cand)))) if cand else 0
    return tokens, word_pos, pos_of, all_cand, cand, target


def swap_plan(text: str, level: float) -> dict[str, int]:
    """Public summary of the swap plan (used by the service's time estimate)."""
    _, word_pos, _, all_cand, cand, target = _swap_plan(text, level)
    return {"words": len(word_pos), "candidates": len(cand), "target": target,
            "skipped_near_number": len(all_cand) - len(cand)}


def _swap_words(
    text: str,
    level: float,
    *,
    backend: str,
    base_url: str,
    model: str,
    api_key: str | None,
    timeout: float,
    temperature: float,
    reasoning_effort: str | None,
    collect_edits: bool = False,
    verify_override: bool | None = None,
    keep_after: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Replace about `level` of the content words, one LLM call per word.

    Settings (env): WATERMARKS_SWAP_WINDOW = words kept on each side of the target
    (default 10), WATERMARKS_SWAP_CTX_TOKENS = prompt budget (default 1200; the window
    shrinks word by word, with a warning, when a prompt would not fit),
    WATERMARKS_SWAP_PROMPT = auto | ru | ru_short | en, WATERMARKS_SWAP_MORPH = 0 to
    skip the pymorphy3 dictionary/form checks, WATERMARKS_SWAP_WORDLIST = file of extra
    words (one per line) the dictionary check should accept. WATERMARKS_SWAP_VERIFY=1
    adds a meaning check per candidate (Ollama): the checker scores log P(yes)/P(no)
    for "same meaning?" and the candidate is kept when the score reaches
    WATERMARKS_SWAP_VERIFY_MIN (higher = stricter, fewer but better swaps; unset = the `verify_min` of the
    checker model in config/model_profiles.json, else 12);
    WATERMARKS_SWAP_VERIFY_MODEL picks another checker model (default: the same one).

    The returned stats carry a `warnings` list (quota not met, window shrunk, checks
    unavailable, failed calls) for the caller to surface.
    """
    n0 = max(1, _env_int("WATERMARKS_SWAP_WINDOW", 10))
    budget = _env_int("WATERMARKS_SWAP_CTX_TOKENS", 1200)
    # One word (or NULL) is expected back: cap the reply and each call's wait so a
    # model that rambles or hangs costs seconds, not the whole request timeout.
    max_reply = max(4, _env_int("WATERMARKS_SWAP_MAX_TOKENS", 24))
    call_timeout = max(5, _env_int("WATERMARKS_SWAP_CALL_TIMEOUT", 120))
    # Optional meaning check of every accepted candidate (Ollama only; one extra call).
    verify = _env("WATERMARKS_SWAP_VERIFY", "0") == "1" if verify_override is None else verify_override
    verify_model = _env("WATERMARKS_SWAP_VERIFY_MODEL") or model
    # an explicit env value wins; otherwise the threshold measured for this checker model
    # (config/model_profiles.json), otherwise the default tuned for YandexGPT
    verify_min = _verify_min_for(verify_model)
    kind = (_env("WATERMARKS_SWAP_PROMPT", "auto") or "auto").lower()
    if kind == "auto":
        kind = "ru" if _script_of(text) == "cyrillic" else "en"
    if kind not in _SWAP_PROMPTS:
        raise ValueError(f"unknown WATERMARKS_SWAP_PROMPT {kind!r}")

    tokens, word_pos, pos_of, all_cand, cand, target = _swap_plan(text, level)

    cyrillic = _script_of(text) == "cyrillic"
    morph = _get_morph() if cyrillic and _env("WATERMARKS_SWAP_MORPH", "1") != "0" else None
    vocab = {t.lower() for t in tokens if t.isalpha()}
    wordlist = _env("WATERMARKS_SWAP_WORDLIST")
    if wordlist and os.path.exists(wordlist):
        vocab |= {ln.strip().lower() for ln in Path(wordlist).read_text(encoding="utf-8").splitlines()}

    stats: dict[str, Any] = {
        "words": len(word_pos), "candidates": len(cand),
        "skipped_near_number": len(all_cand) - len(cand), "target": target,
        "accepted": 0, "refused": 0, "invalid": 0, "dict_rejected": 0, "form_rejected": 0,
        "calls": 0, "errors": 0, "ctx_shrunk": 0, "est_tokens_max": 0, "window": n0,
        "prompt": kind, "morph": morph is not None, "warnings": [],
        "verify": False, "verify_calls": 0, "verify_rejected": 0,
    }
    if verify and backend != "ollama":
        stats["warnings"].append("swap: meaning check needs the ollama backend and was skipped")
        verify = False
    stats["verify"] = verify
    if verify:
        stats["verify_min"] = verify_min
        if not _env("WATERMARKS_SWAP_VERIFY_MIN") and not _has_profile_threshold(verify_model):
            stats["warnings"].append(
                f"swap: meaning-check threshold for model {verify_model!r} was not measured, using {verify_min:g} "
                "(tuned for YandexGPT); the check may reject too much or too little - see experiments/pick_thresholds.py"
            )
    if cyrillic and morph is None and _env("WATERMARKS_SWAP_MORPH", "1") != "0":
        stats["warnings"].append(
            "swap: pymorphy3 is not installed, so replacements are not checked against a "
            "dictionary or for grammatical form"
        )
    # Keep the model resident while the calls run, then restore the configured
    # keep-alive (e.g. 0 = unload) once after the last one.
    final_keep = keep_after if keep_after is not None else _env("WATERMARKS_REWRITE_KEEP_ALIVE")
    loop_keep = "5m" if backend == "ollama" else None
    done: dict[int, str] = {}  # token index -> accepted replacement
    dead: set[int] = set()  # asked and refused/invalid: never asked again
    edit_log: list[tuple[int, str, str, float | None]] = []  # (token, old, new, check score)

    def neighbours(i: int) -> list[int]:
        p = pos_of[i]
        return [word_pos[q] for q in (p - 1, p + 1) if 0 <= q < len(word_pos)]

    try:
        for _ in range(_SWAP_MAX_ROUNDS):
            # Adjacent words are never both replaced, so the text stays fluent.
            pool = [
                i for i in cand
                if i not in done and i not in dead and not any(j in done for j in neighbours(i))
            ]
            need = target - len(done)
            if need <= 0 or not pool:
                break
            for i in _spread(pool, min(len(pool), need)):
                if len(done) >= target:
                    break
                if any(j in done for j in neighbours(i)):
                    continue
                p, n = pos_of[i], n0
                while True:
                    lo = word_pos[max(0, p - n)]
                    hi = word_pos[min(len(word_pos) - 1, p + n)]
                    window = (
                        "".join(tokens[lo:i]) + f"[[{tokens[i]}]]" + "".join(tokens[i + 1 : hi + 1])
                    ).strip()
                    prompt = _swap_prompt(kind, window, tokens[i])
                    est = len(prompt) / _SWAP_CHARS_PER_TOKEN
                    if est <= budget or n <= 1:
                        break
                    n -= 1
                if n < n0:
                    stats["ctx_shrunk"] += 1
                    eprint(
                        f"swap: context budget {budget} tokens exceeded around "
                        f"{tokens[i]!r}; window {n0} -> {n} words"
                        + ("" if est <= budget else f" (still ~{est:.0f} tokens)")
                    )
                stats["est_tokens_max"] = max(stats["est_tokens_max"], int(est))
                try:
                    stats["calls"] += 1
                    reply = _generate_once(
                        backend, base_url, model, api_key, prompt,
                        min(timeout, call_timeout), min(temperature, 0.3),
                        reasoning_effort, keep_alive=loop_keep,
                        max_tokens=max_reply,
                    )
                except Exception as exc:  # transport failure: skip this word for now
                    stats["errors"] += 1
                    stats["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
                    continue
                guess = _parse_swap_reply(reply)
                if guess is None:
                    stats["refused"] += 1
                    dead.add(i)
                    continue
                new = _valid_swap(tokens[i], guess)
                if new is None:
                    stats["invalid"] += 1
                    dead.add(i)
                    continue
                if morph is not None:
                    low = new.lower()
                    if " " in new or not (morph.word_is_known(low) or low in vocab):
                        stats["dict_rejected"] += 1
                        dead.add(i)
                        continue
                    if not _form_ok(tokens[i], new):
                        stats["form_rejected"] += 1
                        dead.add(i)
                        continue
                if verify:
                    lo5 = word_pos[max(0, p - _VERIFY_WORDS)]
                    hi5 = word_pos[min(len(word_pos) - 1, p + _VERIFY_WORDS)]
                    vprompt = _verify_prompt(
                        "".join(tokens[lo5:i]).strip(), tokens[i], new,
                        "".join(tokens[i + 1 : hi5 + 1]).strip(),
                    )
                    try:
                        stats["verify_calls"] += 1
                        score = _verify_score(base_url, verify_model, vprompt,
                                              min(timeout, call_timeout), loop_keep)
                    except Exception as exc:  # checker unreachable: skip this word for now
                        stats["errors"] += 1
                        stats["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
                        continue
                    if score < verify_min:
                        stats["verify_rejected"] += 1
                        dead.add(i)
                        continue
                if collect_edits:
                    edit_log.append((i, tokens[i], new, round(score, 2) if verify else None))
                done[i] = new
                tokens[i] = new  # later windows already see this edit
                stats["accepted"] += 1
    finally:
        if backend == "ollama" and final_keep is not None:
            try:
                keep = int(final_keep) if final_keep.lstrip("-").isdigit() else final_keep
                _http_json(base_url.rstrip("/") + "/api/generate",
                           {"model": model, "keep_alive": keep}, {}, 30)
            except Exception:  # best effort: Ollama's own timer unloads it anyway
                pass
    if stats["calls"] and stats["errors"] >= stats["calls"]:
        raise RuntimeError("swap: every LLM call failed")
    warnings = stats["warnings"]
    if stats["accepted"] < target:
        warnings.append(
            f"swap: quota not met, {stats['accepted']} of {target} words replaced "
            f"(model refused {stats['refused']}, rejected {stats['invalid']} unusable, "
            f"{stats['dict_rejected']} not in dictionary, {stats['form_rejected']} wrong form"
            + (f", {stats['verify_rejected']} failed the meaning check" if verify else "")
            + ")"
        )
    if stats["ctx_shrunk"]:
        warnings.append(
            f"swap: prompt budget of {budget} tokens was exceeded for {stats['ctx_shrunk']} "
            f"words; their context window was reduced"
        )
    if stats["errors"]:
        warnings.append(
            f"swap: {stats['errors']} LLM calls failed and were skipped"
            + (f" (last error: {stats['last_error']})" if stats.get("last_error") else "")
        )
    for w in warnings:
        eprint(w)
    if collect_edits:
        # Character offsets are valid for the text this function returns.
        start, offset = [], 0
        for t in tokens:
            start.append(offset)
            offset += len(t)
        stats["edits"] = [
            {
                "id": n, "offset": start[i], "orig": old, "new": new, "score": sc,
                "left": "".join(tokens[word_pos[max(0, pos_of[i] - 5)] : i]).strip(),
                "right": "".join(
                    tokens[i + 1 : word_pos[min(len(word_pos) - 1, pos_of[i] + 5)] + 1]
                ).strip(),
            }
            for n, (i, old, new, sc) in enumerate(sorted(edit_log))
        ]
    return "".join(tokens), stats


def _style_clause(style: str) -> str:
    """The style instruction appended to a rewrite prompt.

    Intended for the humanize / manual-polish tactics (e.g. "write like
    Hemingway"). A request, not a contract: the model may only approximate a
    style, and the fact/voice rules still apply.
    """
    return (
        f"Apply this writing style throughout the rewrite: {style}. Keep the "
        "style subordinate to the content — preserve all facts, numbers, names, "
        "and technical identifiers, and do not add or remove claims."
    )


def _language_clause(text: str) -> str:
    """Instruction pinning the output language to the input's.

    Names Russian explicitly when the text is mostly Cyrillic; otherwise falls
    back to a generic same-language rule.
    """
    letters = [c for c in text if c.isalpha()]
    cyrillic = sum(1 for c in letters if "Ѐ" <= c <= "ӿ")
    if letters and cyrillic / len(letters) > 0.5:
        return (
            "Текст ниже написан на русском языке. Результат должен быть полностью на "
            "русском языке: не переводи текст на английский и не вставляй английские "
            "слова. (The text is in Russian; the output must be Russian only.)"
        )
    return (
        "Write the output in the same language as the input text. Do not translate "
        "it into another language."
    )


def build_prompt(
    tactic: str | None,
    text: str,
    *,
    lang: str = "French",
    original_lang: str = "English",
    rewrite_level: float | None = None,
    style: str | None = None,
) -> str:
    """Construct the LLM rewrite prompt for a given tactic and intensity."""
    if tactic is None:
        if rewrite_level is not None:
            base = PROMPTS["level"].format(TEXT=text, LEVEL=rewrite_level)
        else:
            raise ValueError("unknown tactic: None")
    else:
        base = _tactic_prompt(tactic, text, lang, original_lang)
        # A (tactic, intensity) pair: modulate the named tactic prompt with the
        # level instead of replacing it with the generic level-only prompt. Code is
        # exempt — identifier/comment rewrites are not naturally intensity-modulated.
        if rewrite_level is not None and tactic != "code":
            base = base + "\n\n" + _intensity_clause(rewrite_level)
    if style:
        base = base + "\n\n" + _style_clause(style)
    # Translation tactics change language on purpose; every other rewrite must
    # keep the input's language (small local models otherwise drift to English).
    if tactic not in ("backtranslate", "code"):
        base = _language_clause(text) + "\n\n" + base
    return base


def _split_units(text: str) -> list[tuple[str, str]]:
    """Split a document into (unit, separator) pairs.

    Breaks after sentence punctuation or on any blank-line / newline run, so
    each fragment is rewritten independently (a fresh context per fragment ⇒
    new per-token watermark keys). Punctuation is kept with its fragment. The
    separator is the whitespace/blank-line run that follows a unit ('' for the
    last); unshuffled chunk mode reassembles with it so paragraph/line layout
    is preserved, while shuffled mode drops it.
    """
    parts = re.split(r"((?<=[.!?])\s+|\n+)", text)
    pairs: list[tuple[str, str]] = []
    i = 0
    while i < len(parts):
        unit = parts[i]
        sep = parts[i + 1] if i + 1 < len(parts) else ""
        if unit.strip() or "\n" in sep:
            pairs.append((unit.strip(), sep))
        i += 2
    return pairs


def _http_json(url: str, payload: dict, headers: dict[str, str], timeout: float) -> dict:
    """Perform an HTTP POST request and return JSON response."""
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError(f"refusing non-http(s) rewrite endpoint: {url}")
    body = json.dumps(payload).encode("utf-8")
    # S310: URL scheme is restricted to http/https just above.
    req = urllib.request.Request(  # noqa: S310
        url,
        data=body,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def call_ollama(
    base_url: str,
    model: str,
    prompt: str,
    timeout: float,
    temperature: float,
    keep_alive: str | int | None = None,
    max_tokens: int | None = None,
) -> str:
    """Call Ollama API endpoint for text rewrite.

    `keep_alive` overrides WATERMARKS_REWRITE_KEEP_ALIVE for this call (multi-call
    steps keep the model resident between calls and unload it after the last one).
    `max_tokens` caps the reply length (Ollama's num_predict) so a model that fails
    to stop cannot stall a short-answer call until the timeout.
    """
    url = base_url.rstrip("/") + "/api/chat"
    options: dict[str, Any] = {"temperature": temperature}
    if max_tokens is not None:
        options["num_predict"] = max_tokens
    # Ollama's default context is small (~4k tokens): a long input plus its rewrite
    # silently truncates, so allow raising it.
    num_ctx = _env_int("WATERMARKS_REWRITE_NUM_CTX", 0)
    if num_ctx > 0:
        options["num_ctx"] = num_ctx
    payload: dict[str, Any] = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
        "options": options,
    }
    # keep_alive: seconds (0 = unload the model right after the reply) or a
    # duration string such as "30s". Ollama's default keeps it loaded for 5 min.
    if keep_alive is None:
        keep_alive = _env("WATERMARKS_REWRITE_KEEP_ALIVE")
    if keep_alive is not None:
        keep_alive = str(keep_alive)
        payload["keep_alive"] = int(keep_alive) if keep_alive.lstrip("-").isdigit() else keep_alive
    data = _http_json(url, payload, {}, timeout)
    msg = data.get("message") or {}
    content = msg.get("content")
    if not content:
        raise RuntimeError(f"ollama empty response: {data!r}"[:500])
    return str(content).strip()


def call_openai_compatible(
    base_url: str,
    model: str,
    prompt: str,
    api_key: str | None,
    timeout: float,
    temperature: float,
    reasoning_effort: str | None = None,
) -> str:
    """Call OpenAI-compatible chat completions API."""
    url = base_url.rstrip("/") + "/v1/chat/completions"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    data = _http_json(
        url,
        payload,
        headers,
        timeout,
    )
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"openai-compatible empty choices: {data!r}"[:500])
    content = (choices[0].get("message") or {}).get("content")
    if not content:
        raise RuntimeError(f"openai-compatible empty content: {data!r}"[:500])
    return str(content).strip()


def _candidate_pass(
    evaluation: dict, target_margin: float
) -> tuple[bool | None, float | None, float | None]:
    """Judge a detection report against a score-margin objective.

    Returns (passed, margin, raw_margin). ``passed`` is True when the detector
    reports the text not-watermarked AND its score sits at least ``target_margin``
    below the threshold; False when still watermarked; None when no verdict is
    available (fail-soft) or when the margin floor is not met. ``margin`` =
    round(threshold - score, 4), or None when either field is missing OR the
    threshold is not a score-scale cutoff. ``raw_margin`` is the unrounded
    margin, or None in the same cases where ``margin`` is None; it lets ranking
    compare candidates without the loss from rounding to four decimals.

    Some detectors (keyed-Gumbel) report a *p-value* threshold that does not
    scale with their ``score``, so ``threshold - score`` is meaningless; a
    not-watermarked report whose score is above such a threshold is treated as
    a clear pass rather than a gated margin.
    """
    verdict = evaluation.get("is_watermarked")
    if verdict is None:
        return None, None, None
    score = evaluation.get("score")
    threshold = evaluation.get("threshold")
    margin = None
    raw_margin = None
    if isinstance(score, (int, float)) and isinstance(threshold, (int, float)):
        raw_margin = float(threshold) - float(score)
        margin = round(raw_margin, 4)
    if verdict is True:
        return False, margin, raw_margin
    # Not watermarked. If the threshold is not a score-scale cutoff it cannot
    # express a margin, so fall back to a clean pass (no margin floor).
    if raw_margin is not None and raw_margin < 0:
        margin = None
        raw_margin = None
    met = raw_margin is not None and raw_margin >= target_margin - 1e-9
    if raw_margin is None or met:
        return True, margin, raw_margin
    return None, margin, raw_margin


def _margin_of(rec: dict) -> tuple[float, float, float]:
    # Rank by the unrounded margin first: the telemetry margin is rounded to
    # four decimals, so two candidates can share that rounded value while
    # differing in the raw margin (e.g. 0.12343 vs 0.12344 both round to
    # 0.1234). Preserve the p-value and lexical-divergence tie-breakers.
    """Compute rankable candidate margin score."""
    raw = rec.get("raw_margin")
    raw_val = float(raw) if raw is not None else -float("inf")
    # For evaluators that report p_value (e.g. Keyed-Gumbel), lower p_value indicates a safer pass
    eval_rec = rec.get("evaluation") or {}
    pval = eval_rec.get("p_value")
    neg_pval = -float(pval) if isinstance(pval, (int, float)) else -float("inf")
    # Secondary tiebreaker: prefer less divergence
    div = -float(rec.get("lexical_divergence", 0.0))
    return (raw_val, neg_pval, div)


def rewrite(
    text: str,
    *,
    backend: str,
    model: str | None,
    base_url: str | None,
    api_key: str | None,
    tactic: str,
    lang: str,
    original_lang: str,
    timeout: float,
    layer_a_after: bool,
    temperature: float,
    candidates: int,
    max_loops: int = 1,
    allow_remote: bool = False,
    reasoning_effort: str | None = None,
    markllm_scheme: str | None = None,
    markllm_dir: str | None = None,
    markllm_model: str | None = None,
    markllm_timeout: float = 180.0,
    gumbel_key: str | None = None,
    rewrite_level: float | None = None,
    style: str | None = None,
    target_margin: float = 0.0,
    selection: str = "min-divergence",
    chunk_shuffle: bool = False,
    noop_lex_floor: float = 0.05,
) -> tuple[str, dict]:
    """Execute text rewrite pass across candidates and select best candidate."""
    prompt = build_prompt(
        tactic,
        text,
        lang=lang,
        original_lang=original_lang,
        rewrite_level=rewrite_level,
        style=style,
    )
    info: dict = {
        "backend": backend,
        "tactic": tactic,
        "rewrite_level": rewrite_level,
        "style": style,
        "target_margin": target_margin,
        "selection": selection,
        "noop_lex_floor": noop_lex_floor,
        "noop": False,
        "model": model,
        "base_url": base_url,
        "temperature": temperature,
        "prompt_chars": len(prompt),
        "input_chars": len(text),
    }
    if reasoning_effort:
        info["reasoning_effort"] = reasoning_effort

    markllm: dict | None = None
    markllm_detector: MarkLLMTextDetector | None = None
    if markllm_scheme:
        markllm_detector = MarkLLMTextDetector(
            scheme=markllm_scheme,
            upstream_dir=markllm_dir,
            model=markllm_model or DEFAULT_MARKLLM_MODEL,
            timeout=markllm_timeout,
        )
        markllm = {
            "scheme": markllm_scheme,
            "before": _safe_detect(markllm_detector, text),
        }
        if not markllm["before"]["available"]:
            eprint(f"markllm verification unavailable: {markllm['before']['error']}")
        info["markllm"] = markllm

    gumbel: dict | None = None
    gumbel_detector: GumbelTextDetector | None = None
    if gumbel_key:
        gumbel_detector = GumbelTextDetector(key=gumbel_key)
        gumbel = {"before": _safe_detect(gumbel_detector, text)}
        if not gumbel["before"]["available"]:
            eprint(f"gumbel verification unavailable: {gumbel['before']['error']}")
        info["gumbel"] = gumbel

    if backend == "print-prompt":
        info["mode"] = "print-prompt"
        if candidates > 1:
            eprint("note: --candidates ignored in print-prompt mode")
        return prompt, info

    if not model and tactic != "mlm":
        raise SystemExit("error: --model required for ollama/openai-compatible backends")
    if not base_url and tactic != "mlm":
        raise SystemExit("error: --base-url required for ollama/openai-compatible backends")

    # The mlm tactic never talks to a remote endpoint; base_url may be None.
    if base_url is not None:
        _check_remote(base_url, allow_remote)

    n_cands = max(1, candidates)
    n_loops = max(1, max_loops)
    info["candidates"] = n_cands
    info["max_loops"] = n_loops
    evaluator_name, evaluator = _pick_evaluator(markllm_detector, gumbel_detector)
    info["evaluator"] = evaluator_name

    is_chunk = tactic == "chunk"
    is_mlm = tactic == "mlm"
    info["chunked"] = is_chunk
    info["chunk_shuffle"] = bool(chunk_shuffle)
    info["mlm"] = is_mlm

    def _rewrite_unit(unit: str) -> str:
        """Rewrite a single text unit with prompt formatting."""
        return _generate_once(
            backend,
            base_url,
            model,
            api_key,
            build_prompt(
                tactic,
                unit,
                lang=lang,
                original_lang=original_lang,
                rewrite_level=rewrite_level,
                style=style,
            ),
            timeout,
            temperature,
            reasoning_effort,
        )

    def _generate_candidate() -> str:
        """Generate a single rewrite candidate via configured backend."""
        if is_mlm:
            return _mlm_infill(text, rewrite_level or 0.3)
        if is_chunk:
            pairs = _split_units(text)
            if chunk_shuffle:
                units = [unit for unit, _ in pairs if unit]
                random.shuffle(units)
                return " ".join(_rewrite_unit(unit) for unit in units)
            # Skip rewriting empty leading units (blank lines at the top) but
            # keep their separators so the reassembled document keeps the layout.
            return "".join((_rewrite_unit(unit) if unit else "") + sep for unit, sep in pairs)
        return _generate_once(
            backend, base_url, model, api_key, prompt, timeout, temperature, reasoning_effort
        )

    # Iterative rewrite: each loop generates --candidates variants and
    # evaluates them ALL (so the best is chosen, not the first to squeak under
    # the threshold). A variant "passes" when the detector reports it
    # not-watermarked AND its score sits at least --target-margin below the
    # threshold; --max-loops caps the evaluation rounds before the best-effort
    # variant is returned. When no detector is configured the evaluator is
    # lexical divergence, which has no pass/fail verdict, so every attempt is
    # generated and the most diverged one is selected (an unguided best-effort).
    attempts: list[tuple[str, dict]] = []
    passed: bool | None = None
    for loop in range(n_loops):
        loop_passed = False
        for _ in range(n_cands):
            cand = _generate_candidate()
            if tactic == "humanize":
                cand = humanize_pass(cand)
            cand_stats: dict | None = None
            if layer_a_after:
                cand, cand_stats = clean_text(cand)
            divergence = _lexical_divergence(text, cand)
            if evaluator is None:
                evaluation: dict = {
                    "evaluator": "lexical-divergence",
                    "score": round(divergence, 4),
                }
            else:
                evaluation = _safe_detect(evaluator, cand)
            passed_i, margin, raw_margin = _candidate_pass(evaluation, target_margin)
            score = evaluation.get("score")
            threshold = evaluation.get("threshold")
            attempts.append(
                (
                    cand,
                    {
                        "loop": loop,
                        "lexical_divergence": round(divergence, 4),
                        "selection_score": round(divergence, 4),
                        "score_after": round(float(score), 4)
                        if isinstance(score, (int, float))
                        else None,
                        "threshold": round(float(threshold), 4)
                        if isinstance(threshold, (int, float))
                        else None,
                        "margin": margin,
                        "raw_margin": raw_margin,
                        "selected": False,
                        "passed": passed_i,
                        "evaluation": evaluation,
                        "layer_a_after": cand_stats,
                    },
                )
            )
            if passed_i is True:
                loop_passed = True
        if loop_passed:
            passed = True
            break

    if evaluator is not None and passed is None:
        passed = False
    info["attempts_made"] = len(attempts)
    info["passed"] = passed

    # Best-effort selection: among the candidates that passed (met the margin
    # objective) pick the one that changed the least (min-divergence, the
    # content-preserving default) or the one with the largest margin
    # (--select max-margin, robustness-first). When none passed, fall back to
    # the lowest watermark score (detector evaluator) or the most diverged
    # variant (unguided fallback).
    selected_idx: int
    best_score: float | None = None
    best_score_idx: int | None = None
    best_div = -1.0
    best_div_idx = 0
    for i, (_cand, rec) in enumerate(attempts):
        if rec["lexical_divergence"] > best_div:
            best_div = rec["lexical_divergence"]
            best_div_idx = i
        if evaluator is not None:
            s = rec["evaluation"].get("score")
            if isinstance(s, (int, float)) and (best_score is None or s < best_score):
                best_score = float(s)
                best_score_idx = i
    passed_idxs = [i for i, (_c, r) in enumerate(attempts) if r["passed"] is True]
    if passed_idxs:
        if selection == "max-margin":
            selected_idx = max(passed_idxs, key=lambda i: _margin_of(attempts[i][1]))
        else:
            selected_idx = min(passed_idxs, key=lambda i: attempts[i][1]["lexical_divergence"])
    elif best_score_idx is not None:
        selected_idx = best_score_idx
    else:
        selected_idx = best_div_idx

    out, rec = attempts[selected_idx]
    rec["selected"] = True
    info["candidate_scores"] = [r for _c, r in attempts]
    if layer_a_after:
        info["layer_a_after"] = rec["layer_a_after"]
    info["output_chars"] = len(out)
    info["mode"] = "rewritten"

    # No-op guard: a rewrite that changed almost nothing is not a removal
    # attempt. Report it so a benchmark never counts a near-verbatim output as
    # "0% clear" (the misleading backtranslate row). Disabled with floor <= 0.
    _out_div = _lexical_divergence(text, out)
    _is_noop = noop_lex_floor > 0 and _out_div < noop_lex_floor
    info["noop"] = bool(_is_noop)
    if _is_noop:
        eprint(
            f"warning: rewrite returned ≈ input (lex divergence {_out_div:.4f} < "
            f"{noop_lex_floor:.4f}); treating as no-op"
        )
    note = (
        "Layer B is best-effort against statistical token-sampling watermarks; "
        "cannot certify removal against a vendor detector."
    )
    if evaluator is not None and passed is not True:
        margin_suffix = f" (target margin {target_margin:.2f})" if target_margin else ""
        note += (
            f" Exhausted {len(attempts)} attempt(s) without passing "
            f"{evaluator_name} evaluation{margin_suffix}; returned the best-effort variant."
        )
    if markllm_scheme:
        note += (
            " Cross-model hygiene: rewrite with a model that is neither the "
            "generator nor itself watermarked, or the rewritten text can be "
            "re-stamped."
        )
    info["note"] = note

    if markllm:
        assert markllm_detector is not None  # set together with markllm above
        if evaluator_name == "markllm":
            # The loop already scored the selected attempt; reuse the verdict
            # instead of paying another MarkLLM detection.
            after = rec["evaluation"]
        else:
            after = _safe_detect(markllm_detector, out)
        markllm["after"] = after
        before = markllm["before"]
        if before.get("available") and after.get("available"):
            markllm["cleared"] = bool(
                before.get("is_watermarked") and not after.get("is_watermarked")
            )
        else:
            markllm["cleared"] = None
        a_score = after.get("score")
        a_thr = after.get("threshold")
        markllm["score_after"] = (
            round(float(a_score), 4) if isinstance(a_score, (int, float)) else None
        )
        markllm["margin"] = (
            round(float(a_thr) - float(a_score), 4)
            if isinstance(a_score, (int, float)) and isinstance(a_thr, (int, float))
            else None
        )
        markllm["note"] = (
            "MarkLLM detection is only valid against the SAME scheme config + "
            "keys used at generation; it does not certify a vendor detector."
        )

    if gumbel:
        assert gumbel_detector is not None  # set together with gumbel above
        if evaluator_name == "gumbel":
            # The loop already scored the selected attempt; reuse the verdict
            # instead of paying another replay.
            after = rec["evaluation"]
        else:
            after = _safe_detect(gumbel_detector, out)
        gumbel["after"] = after
        before = gumbel["before"]
        if before.get("available") and after.get("available"):
            gumbel["cleared"] = bool(
                before.get("is_watermarked") and not after.get("is_watermarked")
            )
        else:
            gumbel["cleared"] = None
        gumbel["note"] = (
            "Keyed-Gumbel detection is a same-key replay: valid only with the "
            "same key, tokenizer, and PRF layout used at generation; it does "
            "not certify a vendor detector."
        )

    eprint(
        f"note: evaluator={evaluator_name} attempts={len(attempts)}/"
        f"{n_cands * n_loops} loops={n_loops} passed={passed}"
    )
    return out, info


# Tactics accepted by a strategy spec. `mlm` is a local masked-LM edit; the
# rest go through the configured rewrite backend.
KNOWN_TACTICS = frozenset(
    {"paraphrase", "backtranslate", "structural", "humanize", "code", "chunk", "swap", "mlm"}
)
LLM_TACTICS = frozenset(KNOWN_TACTICS - {"mlm"})


def parse_strategy(spec: str) -> list[tuple[str, float]]:
    """Parse a strategy like ``"paraphrase@0.8,mlm@0.2"`` -> [(tactic, intensity)].

    Validates tactic names and that intensity lies in (0,1]. Raises ValueError on
    malformed input (callers treat a bad strategy as a request error).
    """
    if not spec or not spec.strip():
        raise ValueError("strategy must be a non-empty list of tactic@intensity steps")
    steps: list[tuple[str, float]] = []
    for raw in spec.split(","):
        item = raw.strip()
        if not item:
            raise ValueError(f"bad strategy step {item!r}; expected tactic@intensity")
        if "@" not in item:
            raise ValueError(f"bad strategy step {item!r}; expected tactic@intensity")
        tactic, raw_level = item.rsplit("@", 1)
        tactic = tactic.strip()
        if tactic not in KNOWN_TACTICS:
            raise ValueError(f"unknown strategy tactic {tactic!r}")
        try:
            level = float(raw_level)
        except ValueError:
            raise ValueError(f"bad intensity in strategy step {item!r}") from None
        if not (0 < level <= 1):
            raise ValueError(f"strategy intensity must be in (0,1], got {level} in {item!r}")
        steps.append((tactic, level))
    return steps


_PARA_SPLIT = re.compile(r"(\n[ \t]*\n+)")


def _per_paragraph() -> bool:
    """Paraphrase paragraph by paragraph (default): a model asked to rewrite a whole text tends to
    merge short paragraphs, which breaks rebuilding a document from the result."""
    return _env("WATERMARKS_PARAPHRASE_PER_PARAGRAPH", "1") != "0"


def _paragraph_parts(text: str) -> list[str] | None:
    """Split into [para, sep, para, ...]; None when there is only one paragraph."""
    parts = _PARA_SPLIT.split(text)
    return parts if len([p for p in parts[::2] if p.strip()]) > 1 else None


def _paraphrase_paragraphs(
    parts: list[str], gen: Any, *, keep_model: bool, final_keep: str | None, base_url: str, model: str
) -> tuple[str, dict[str, int]]:
    """Paraphrase each paragraph on its own and put the same separators back.

    Very short paragraphs (captions, headings, formula labels) stay as they are. A paragraph the
    model returns empty or as several paragraphs is kept as one (joined) or original paragraph, so
    the paragraph count never changes.
    """
    min_words = max(1, _env_int("WATERMARKS_PARAPHRASE_MIN_WORDS", 8))
    out, st = [], {"total": 0, "rewritten": 0, "kept_short": 0, "kept_failed": 0, "joined": 0}
    try:
        for k, piece in enumerate(parts):
            if k % 2 == 1 or not piece.strip():
                out.append(piece)
                continue
            st["total"] += 1
            if len(piece.split()) < min_words:
                st["kept_short"] += 1
                out.append(piece)
                continue
            lead = piece[: len(piece) - len(piece.lstrip())]
            trail = piece[len(piece.rstrip()):]
            try:
                new = gen(piece.strip(), "5m" if keep_model else None).strip()
            except Exception:  # noqa: BLE001 - one failed paragraph must not lose the whole text
                new = ""
            if not new:
                st["kept_failed"] += 1
                out.append(piece)
                continue
            if _PARA_SPLIT.search(new):
                st["joined"] += 1
                new = " ".join(new.split())
            out.append(lead + new + trail)
            st["rewritten"] += 1
    finally:
        if keep_model and final_keep is not None:
            try:
                keep = int(final_keep) if final_keep.lstrip("-").isdigit() else final_keep
                _http_json(base_url.rstrip("/") + "/api/generate", {"model": model, "keep_alive": keep}, {}, 30)
            except Exception:  # best effort: Ollama's own timer unloads it anyway
                pass
    return "".join(out), st


def apply_strategy(
    text: str,
    steps: list[tuple[str, float]],
    *,
    backend: str,
    model: str | None,
    base_url: str | None,
    api_key: str | None,
    timeout: float = 120.0,
    temperature: float = 0.9,
    reasoning_effort: str | None = None,
    lang: str = "French",
    original_lang: str = "English",
    style: str | None = None,
    layer_a_after: bool = False,
    collect_edits: bool = False,
    swap_verify: bool | None = None,
    keep_after: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Apply a strategy's steps sequentially to *text* (best-effort rewrite).

    Unlike ``rewrite()`` this does not run a detection/evaluation loop — each
    step is applied exactly once and feeds the next, so it suits an operation
    that wants the rewrite regardless of a removal verdict. ``mlm`` steps use a
    local masked-LM edit; every other tactic makes one backend generation via
    ``build_prompt``/``_generate_once``. Returns (final_text, stats) where stats
    carries per-step tactic/intensity/input-output lengths.
    """
    needs_llm = any(t in LLM_TACTICS for t, _ in steps)
    if needs_llm:
        if backend not in ("openai-compatible", "ollama"):
            raise RuntimeError(f"strategy needs an LLM backend, got {backend!r}")
        if not model or not base_url:
            raise RuntimeError("strategy needs --model and --base-url for LLM steps")

    cur = text
    step_stats: list[dict[str, Any]] = []
    for step_no, (tactic, intensity) in enumerate(steps):
        in_chars = len(cur)
        words_in = len(cur.split())
        t_step = time.monotonic()
        extra: dict[str, Any] = {}
        if tactic == "mlm":
            cur = _mlm_infill(cur, intensity)
        elif tactic == "swap":
            cur, swap_stats = _swap_words(
                cur,
                intensity,
                backend=backend,
                base_url=base_url,
                model=model,
                api_key=api_key,
                timeout=timeout,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                collect_edits=collect_edits,
                verify_override=swap_verify,
                keep_after=keep_after,
            )
            if collect_edits:
                # Offsets refer to this step's output: only usable when nothing changes the
                # text afterwards.
                swap_stats["offsets_valid"] = step_no == len(steps) - 1 and not layer_a_after
            extra["swap"] = swap_stats
        else:
            def _whole(piece: str, keep: str | int | None = None) -> str:
                prompt = build_prompt(
                    tactic,
                    piece,
                    lang=lang,
                    original_lang=original_lang,
                    rewrite_level=intensity,
                    style=style,
                )
                extra_kw = {} if keep is None else {"keep_alive": keep}
                return _generate_once(
                    backend, base_url, model, api_key, prompt, timeout, temperature,
                    reasoning_effort, **extra_kw,
                )

            parts = _paragraph_parts(cur) if tactic == "paraphrase" and _per_paragraph() else None
            if parts is None:
                cur = _whole(cur, keep_after)
            else:
                cur, para_stats = _paraphrase_paragraphs(
                    parts, _whole, keep_model=backend == "ollama",
                    final_keep=keep_after if keep_after is not None else _env("WATERMARKS_REWRITE_KEEP_ALIVE"),
                    base_url=base_url, model=model,
                )
                extra["paragraphs"] = para_stats
        step_stats.append(
            {
                "tactic": tactic,
                "intensity": round(intensity, 4),
                "in_chars": in_chars,
                "out_chars": len(cur),
                "words": words_in,
                "seconds": round(time.monotonic() - t_step, 2),
                **extra,
            }
        )
    # Layer A scrub once, on the complete strategy output (not per step).
    if layer_a_after and cur:
        cur = clean_text(cur)[0]
    return cur, {
        "backend": backend,
        "tactic": "strategy",
        "mode": "strategy",
        "strategy": [f"{t}@{i:g}" for t, i in steps],
        "steps": step_stats,
        "input_chars": len(text),
        "output_chars": len(cur),
    }


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser for text rewrite tool."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("path", nargs="?", default="-", help="Input text file, or - for stdin")
    p.add_argument("-o", "--output", help="Output path (default: stdout or *.rewritten.*)")
    p.add_argument(
        "--backend",
        choices=("print-prompt", "ollama", "openai-compatible"),
        default=_env("WATERMARKS_REWRITE_BACKEND", "print-prompt"),
    )
    p.add_argument("--model", default=_env("WATERMARKS_REWRITE_MODEL"))
    p.add_argument(
        "--base-url",
        default=_env("WATERMARKS_REWRITE_BASE_URL", "http://127.0.0.1:11434"),
    )
    p.add_argument(
        "--allow-remote",
        action="store_true",
        default=None,
        help="Allow non-loopback rewrite endpoints (default: deny; "
        "WATERMARKS_REWRITE_ALLOW_REMOTE=1 has the same effect)",
    )
    p.add_argument(
        "--reasoning-effort",
        choices=("none", "low", "medium", "high", "off"),
        default=_env("WATERMARKS_REWRITE_REASONING_EFFORT", "none"),
        help="OpenAI-compatible reasoning_effort; 'none' skips chain-of-thought "
        "(reasoning models like deepseek-v4-flash otherwise burn minutes on a "
        "rewrite). 'off' omits the parameter entirely.",
    )
    # NOTE: no --api-key flag on purpose — keys on argv are visible in `ps`
    # and shell history. Set WATERMARKS_REWRITE_API_KEY instead.
    p.add_argument(
        "--tactic",
        choices=(
            "paraphrase",
            "backtranslate",
            "structural",
            "humanize",
            "code",
            "chunk",
            "swap",
            "mlm",
        ),
        default="paraphrase",
    )
    p.add_argument(
        "--strategy",
        default=None,
        help="Ordered tactic@intensity strategy to apply (e.g. "
        "'paraphrase@0.8,mlm@0.2'). When set, applies the whole strategy "
        "sequentially (each step feeds the next) instead of a single --tactic; "
        "no detection/evaluation loop.",
    )
    p.add_argument(
        "--style",
        default=None,
        help="Optional writing-style instruction appended to the rewrite prompt "
        "(e.g. 'write like Hemingway'). Most meaningful with --tactic humanize; "
        "a request, not a guarantee.",
    )
    p.add_argument(
        "--noop-lex-floor",
        type=float,
        default=0.05,
        help="Treat a rewrite that changed fewer than this fraction of bigrams as "
        "a no-op (emitted as noop:true in --json-stats, with a warning; default "
        "0.05, 0 disables). A no-op is not a removal attempt.",
    )
    p.add_argument(
        "--rewrite-level",
        type=float,
        default=None,
        help="Numeric rewrite intensity in (0,1]; 0 (the unchanged original) is "
        "excluded. When set alongside --tactic it modulates that tactic's "
        "prompt with an intensity clause (change roughly this fraction of tokens). "
        "Omit to use the plain --tactic prompt. Planned nominal default 0.5, to "
        "be tuned from benchmark output.",
    )
    p.add_argument(
        "--target-margin",
        type=float,
        default=0.0,
        help="Require a detection to sit at least this far below the threshold "
        "to count as a pass (robustness floor; default 0.0 = any not-watermarked "
        "verdict). Margin = threshold - score.",
    )
    p.add_argument(
        "--select",
        choices=("min-divergence", "max-margin"),
        default="min-divergence",
        help="Among candidates that pass --target-margin, select the one that "
        "changed the least (min-divergence, the content-preserving default) or "
        "the one with the largest score margin (max-margin, robustness-first).",
    )
    p.add_argument(
        "--chunk-shuffle",
        action="store_true",
        help="With --tactic chunk, shuffle the rewritten fragments (breaks "
        "cross-fragment context ordering; destroys document coherence, so "
        "opt-in)",
    )
    p.add_argument("--lang", default="French", help="Pivot language for backtranslate")
    p.add_argument("--original-lang", default="English")
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument(
        "--temperature",
        type=float,
        default=0.9,
        help="Sampling temperature for the rewrite backend",
    )
    p.add_argument(
        "--candidates",
        type=int,
        default=_env_int("WATERMARKS_REWRITE_CANDIDATES", DEFAULT_CANDIDATES),
        help="Variants generated per loop iteration; each variant is one "
        "rewrite + one evaluation, and the loop stops as soon as an attempt "
        f"passes (default: {DEFAULT_CANDIDATES}; WATERMARKS_REWRITE_CANDIDATES)",
    )
    p.add_argument(
        "--max-loops",
        type=int,
        default=_env_int("WATERMARKS_REWRITE_LOOPS", DEFAULT_MAX_LOOPS),
        help="Max evaluation rounds; each round generates --candidates "
        "variants and stops when one passes. Raising this retries new "
        f"variants until an evaluation passes (default: {DEFAULT_MAX_LOOPS}; "
        "WATERMARKS_REWRITE_LOOPS)",
    )
    p.add_argument(
        "--no-layer-a-after",
        action="store_true",
        help="Skip Layer A scrub on model output",
    )
    p.add_argument("--json-stats", action="store_true", help="Stats JSON on stderr")
    p.add_argument(
        "--markllm-scheme",
        # Keep in sync with detect_text_watermark.SCHEMES.
        choices=("kgw", "synthid", "synthid-text", "exp", "unigram", "sir"),
        default=None,
        help="Run MarkLLM before/after detection around the rewrite AND drive "
        "the iterative rewrite loop with it (the evaluator, when configured; "
        "otherwise lexical divergence selects the best variant). "
        "Scheme = any key of detect_text_watermark.SCHEMES.",
    )
    p.add_argument(
        "--markllm-dir",
        default=_env("MARKLLM_DIR"),
        help="MarkLLM checkout root (default: $MARKLLM_DIR)",
    )
    p.add_argument(
        "--markllm-model",
        default=_env("MARKLLM_MODEL", DEFAULT_MARKLLM_MODEL),
        help=f"Scoring model for MarkLLM detection (default: $MARKLLM_MODEL or {DEFAULT_MARKLLM_MODEL})",
    )
    p.add_argument(
        "--markllm-timeout",
        type=float,
        default=float(_env("WATERMARKS_MARKLLM_TIMEOUT", "180.0")),
        help="Timeout per MarkLLM detection call (default: 180.0)",
    )
    p.add_argument(
        "--gumbel-key",
        default=_env("WATERMARKS_GUMBEL_KEY"),
        help="Secret key for keyed-Gumbel (Aaronson EXP) same-key replay "
        "detection; drives the iterative rewrite loop as the evaluator when "
        "set (default: $WATERMARKS_GUMBEL_KEY). Preferred via env — keys on "
        "argv are visible in ps/history; never logged.",
    )
    p.add_argument(
        "--force-text",
        action="store_true",
        help="Rewrite even when the input looks like a binary container",
    )
    return p


def main() -> int:
    """CLI entry point."""
    args = build_parser().parse_args()

    if args.rewrite_level is not None and not (0 < args.rewrite_level <= 1):
        eprint(f"error: --rewrite-level must be in (0,1], got {args.rewrite_level}")
        return 2

    text = read_text_input(args.path, allow_binary=args.force_text)
    allow_remote = (
        args.allow_remote
        if args.allow_remote is not None
        else _flag_env("WATERMARKS_REWRITE_ALLOW_REMOTE")
    )
    steps: list[tuple[str, float]] | None = None
    if args.strategy:
        try:
            steps = parse_strategy(args.strategy)
        except ValueError as e:
            eprint(f"error: {e}")
            return 2
    try:
        if steps is not None:
            # Enforce the remote-endpoint policy for a strategy, same as the
            # single-tactic rewrite path.
            if any(t in LLM_TACTICS for t, _ in steps) and args.base_url:
                _check_remote(args.base_url, allow_remote)
            result, info = apply_strategy(
                text,
                steps,
                backend=args.backend,
                model=args.model,
                base_url=args.base_url,
                api_key=_env("WATERMARKS_REWRITE_API_KEY"),
                timeout=args.timeout,
                temperature=args.temperature,
                reasoning_effort=(
                    None if args.reasoning_effort == "off" else args.reasoning_effort
                ),
                lang=args.lang,
                original_lang=args.original_lang,
                style=args.style,
                layer_a_after=not args.no_layer_a_after,
            )
        else:
            result, info = rewrite(
                text,
                backend=args.backend,
                model=args.model,
                base_url=args.base_url,
                api_key=_env("WATERMARKS_REWRITE_API_KEY"),
                tactic=args.tactic,
                style=args.style,
                lang=args.lang,
                original_lang=args.original_lang,
                timeout=args.timeout,
                layer_a_after=not args.no_layer_a_after,
                temperature=args.temperature,
                candidates=args.candidates,
                max_loops=args.max_loops,
                allow_remote=allow_remote,
                reasoning_effort=(
                    None if args.reasoning_effort == "off" else args.reasoning_effort
                ),
                markllm_scheme=args.markllm_scheme,
                markllm_dir=args.markllm_dir,
                markllm_model=args.markllm_model,
                markllm_timeout=args.markllm_timeout,
                gumbel_key=args.gumbel_key,
                rewrite_level=args.rewrite_level,
                target_margin=args.target_margin,
                selection=args.select,
                chunk_shuffle=args.chunk_shuffle,
                noop_lex_floor=args.noop_lex_floor,
            )
    except (urllib.error.URLError, TimeoutError, RuntimeError) as e:
        eprint(f"rewrite failed: {e}")
        return 1

    out = args.output
    if out is None and args.path not in (None, "-") and args.backend != "print-prompt":
        out = str(cleaned_path(Path(args.path), suffix=".rewritten"))
    elif out is None and args.backend == "print-prompt":
        out = "-"

    write_text_output(result, out)
    if args.json_stats:
        eprint(json.dumps(info, indent=2, ensure_ascii=False))
    else:
        eprint(
            f"backend={info['backend']} tactic={info['tactic']} "
            f"mode={info.get('mode')} evaluator={info.get('evaluator', '-')} "
            f"attempts={info.get('attempts_made', '-')} passed={info.get('passed', '-')} "
            f"chars {info['input_chars']}->{info.get('output_chars', len(result))}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
