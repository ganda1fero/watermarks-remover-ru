#!/usr/bin/env python3
"""Разбор файла на абзацы и сборка обратно: чтобы сервис чистил только текст, а оформление не терялось.

Команды (вывод всегда JSON в stdout):

  extract IN OUTDIR [--min-words 5]
      IN: .docx .md .txt .pdf. Пишет OUTDIR/text.txt (абзацы через пустую строку) и OUTDIR/units.json.
      Абзацы, которые трогать опасно (ссылки, поля, картинки, код, заголовки, таблицы Markdown, короткие
      строки), остаются как есть и перечислены в `skipped`.

  rebuild IN OUTDIR CLEANED_TXT OUT
      Берёт очищенный текст (тот же порядок абзацев, через пустую строку) и подставляет его в копию файла.
      Для .pdf собирается новый .docx (оформление PDF не сохраняется).

  check IN OUT OUTDIR
      Сверяет структуру результата с исходным файлом: число абзацев, таблиц, картинок, стили, нетронутые абзацы.

Зависимости: python-docx (для .docx и .pdf-результата), pymupdf (только для чтения .pdf).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

PARA_SPLIT = re.compile(r"\n[ \t]*\n")
MARKUP_MD = re.compile(r"`|\]\(|!\[|<[A-Za-z/!]|\$|\[\^|\{\{|\[\[")
LIST_RE = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)(.*)$")
HEADING_STYLES = ("heading", "title", "subtitle", "toc", "заголовок", "название", "оглавление", "code", "код")


def _words(s: str) -> int:
    return len(s.split())


# A bibliography entry ("// journal", "URL:", "С. 12", "P. 12") must never be paraphrased: titles, authors and
# page numbers have to stay exactly as published.
REF_RE = re.compile(r"\s//\s|URL:|https?://|[–—-]\s*(?:С|P|pp)\.\s*\d")


def _content_skip_reason(text: str) -> str | None:
    """Paragraphs the Russian rewrite must not touch whatever the file format is."""
    letters = [c for c in text if c.isalpha()]
    if letters and sum(1 for c in letters if "Ѐ" <= c <= "ӿ") / len(letters) < 0.5:
        return "non_russian"
    if REF_RE.search(text):
        return "bibliography"
    return None


def _count(skipped: dict[str, int], reason: str) -> None:
    skipped[reason] = skipped.get(reason, 0) + 1


# ------------------------------------------------------------------ docx


def _docx_paragraphs(doc: Any):
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph

    return [Paragraph(p, doc._body) for p in doc.element.body.iter(qn("w:p"))]


def _docx_skip_reason(par: Any, min_words: int) -> str | None:
    from docx.oxml.ns import qn

    el = par._p
    text = "".join(r.text for r in par.runs)
    if not text.strip():
        return "empty"
    style = (par.style.name if par.style is not None else "").lower()
    if style.startswith(HEADING_STYLES):
        return "heading_or_code_style"
    for tag, reason in (
        # inline equations live between the runs: rewriting the words around them would misplace them
        ("m:oMath", "formula"), ("m:oMathPara", "formula"),
        ("w:hyperlink", "hyperlink"), ("w:fldChar", "field"), ("w:fldSimple", "field"),
        ("w:drawing", "picture"), ("w:pict", "picture"), ("w:footnoteReference", "footnote"),
        ("w:endnoteReference", "footnote"), ("w:commentReference", "comment"), ("w:ins", "tracked_change"),
        ("w:del", "tracked_change"), ("w:sym", "symbol"), ("w:smartTag", "smart_tag"), ("w:sdt", "content_control"),
    ):
        if el.find(".//" + qn(tag)) is not None:
            return reason
    for r in par.runs:
        if r._r.find(qn("w:br")) is not None or r._r.find(qn("w:tab")) is not None:
            return "tab_or_break"
    if len(par.runs) != len(list(el.iterchildren(qn("w:r")))):
        return "unusual_runs"
    if _words(text) < min_words:
        return "short"
    return _content_skip_reason(text)


def _run_format(r: Any) -> tuple:
    f = r.font
    color = f.color.rgb if f.color is not None and f.color.type is not None else None
    return (f.bold, f.italic, f.underline, f.name, f.size, str(color), f.strike, f.superscript, f.subscript)


def extract_docx(path: Path, min_words: int) -> tuple[list[dict], dict]:
    from docx import Document

    doc = Document(str(path))
    units, skipped = [], {}
    for idx, par in enumerate(_docx_paragraphs(doc)):
        why = _docx_skip_reason(par, min_words)
        if why:
            if why != "empty":
                _count(skipped, why)
            continue
        runs = [r for r in par.runs if r.text]
        mixed = len({_run_format(r) for r in runs}) > 1
        units.append({"id": len(units), "index": idx, "text": "".join(r.text for r in par.runs), "mixed_format": mixed})
    return units, skipped


def rebuild_docx(path: Path, units: list[dict], new_texts: list[str], out: Path) -> dict:
    from docx import Document

    doc = Document(str(path))
    pars = _docx_paragraphs(doc)
    flattened = 0
    for u, new in zip(units, new_texts):
        par = pars[u["index"]]
        if "".join(r.text for r in par.runs) != u["text"]:
            raise ValueError(f"абзац {u['id']} в файле уже отличается от разобранного: разбери файл заново")
        if not new.strip() or new == u["text"]:
            continue  # unchanged paragraphs keep their runs, so their formatting stays exactly as it was
        runs = [r for r in par.runs if r.text]
        if not runs:
            continue
        runs[0].text = new
        for r in runs[1:]:
            r.text = ""
        if u.get("mixed_format"):
            flattened += 1
    doc.save(str(out))
    return {"mixed_format_paragraphs_flattened": flattened}


_KEEP_MARK_RE = re.compile(r"<w:(?:keepNext|keepLines|pageBreakBefore)\b([^>]*?)(?:/>|>)")


def docx_keep_marks(path: Path) -> int:
    """Число отметок «Не отрывать от следующего», «Не разрывать абзац», «С новой страницы».

    Word рисует их чёрными квадратиками на полях; в итоговом файле их быть не должно
    (их снимает слой A сервиса). Явно выключенные (w:val="0") квадратик не дают и не считаются.
    """
    import zipfile

    total = 0
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if name.startswith("word/") and name.endswith(".xml"):
                xml = zf.read(name).decode("utf-8", errors="replace")
                for m in _KEEP_MARK_RE.finditer(xml):
                    if not re.search(r"""w:val\s*=\s*["'](?:0|false|off)["']""", m.group(1)):
                        total += 1
    return total


def fingerprint_docx(path: Path) -> dict:
    from docx import Document

    doc = Document(str(path))
    pars = _docx_paragraphs(doc)
    return {
        "paragraphs": len(pars),
        "tables": len(doc.tables),
        "pictures": len(doc.inline_shapes),
        "styles": [(p.style.name if p.style is not None else "") for p in pars],
        "texts": ["".join(r.text for r in p.runs) for p in pars],
        "keep_marks": docx_keep_marks(path),
    }


# ------------------------------------------------------------------ markdown


def extract_md(path: Path, min_words: int) -> tuple[list[dict], dict]:
    lines = path.read_text(encoding="utf-8").split("\n")
    units, skipped = [], {}
    i, n = 0, len(lines)
    if lines and lines[0].strip() == "---":  # front matter
        j = next((k for k in range(1, n) if lines[k].strip() in ("---", "...")), None)
        if j is not None:
            _count(skipped, "front_matter")
            i = j + 1
    fence = None
    block: list[int] = []

    def flush() -> None:
        if not block:
            return
        first = lines[block[0]]
        chunk = [lines[k] for k in block]
        if all(LIST_RE.match(ln) for ln in chunk):  # a list: one unit per item
            for k in block:
                m = LIST_RE.match(lines[k])
                _add_md_unit(units, skipped, [k], m.group(1), m.group(2), min_words)
        elif first.lstrip().startswith("#"):
            _count(skipped, "heading")
        elif first.lstrip().startswith(("|", ">", "<", "    ", "\t", "---", "***", "===", ":::", "[")):
            _count(skipped, "table_quote_html_or_indented")
        elif any(LIST_RE.match(ln) for ln in chunk):
            _count(skipped, "complex_list")
        else:
            _add_md_unit(units, skipped, block[:], "", " ".join(c.strip() for c in chunk), min_words)
        block.clear()

    while i < n:
        ln = lines[i]
        s = ln.strip()
        if fence:
            if s.startswith(fence):
                fence = None
            i += 1
            continue
        if s.startswith(("```", "~~~")):
            flush()
            fence = s[:3]
            _count(skipped, "code_block")
            i += 1
            continue
        if not s:
            flush()
        else:
            block.append(i)
        i += 1
    flush()
    return units, skipped


def _add_md_unit(units: list, skipped: dict, line_nos: list[int], prefix: str, text: str, min_words: int) -> None:
    if MARKUP_MD.search(text):
        _count(skipped, "markup_inside")
    elif _words(text) < min_words:
        _count(skipped, "short")
    elif _content_skip_reason(text):
        _count(skipped, _content_skip_reason(text))
    else:
        units.append({"id": len(units), "lines": line_nos, "prefix": prefix, "text": text})


def rebuild_md(path: Path, units: list[dict], new_texts: list[str], out: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    lines = raw.split("\n")
    drop: set[int] = set()
    for u, new in zip(units, new_texts):
        if not new.strip():
            continue
        first, rest = u["lines"][0], u["lines"][1:]
        lines[first] = u["prefix"] + " ".join(new.split())
        drop.update(rest)
    out.write_text("\n".join(ln for k, ln in enumerate(lines) if k not in drop), encoding="utf-8", newline="")
    return {}


def fingerprint_md(path: Path) -> dict:
    t = path.read_text(encoding="utf-8")
    return {
        "headings": [ln for ln in t.split("\n") if ln.lstrip().startswith("#")],
        "code_fences": sum(1 for ln in t.split("\n") if ln.strip().startswith(("```", "~~~"))),
        "emphasis": [t.count("**"), t.count("__")],
        "links": re.findall(r"\]\([^)]*\)", t),
    }


# ------------------------------------------------------------------ txt


def extract_txt(path: Path, min_words: int) -> tuple[list[dict], dict]:
    text = path.read_text(encoding="utf-8")
    parts = PARA_SPLIT.split(text)
    units, skipped = [], {}
    for k, p in enumerate(parts):
        if not p.strip():
            continue
        if _words(p) < min_words:
            _count(skipped, "short")
            continue
        if _content_skip_reason(p):
            _count(skipped, _content_skip_reason(p))
            continue
        units.append({"id": len(units), "part": k, "text": p})
    return units, skipped


def rebuild_txt(path: Path, units: list[dict], new_texts: list[str], out: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    seps = PARA_SPLIT.findall(text)
    parts = PARA_SPLIT.split(text)
    for u, new in zip(units, new_texts):
        if new.strip():
            parts[u["part"]] = new
    res = parts[0]
    for sep, part in zip(seps, parts[1:]):
        res += sep + part
    out.write_text(res, encoding="utf-8", newline="")
    return {}


def fingerprint_txt(path: Path) -> dict:
    return {"paragraphs": len([p for p in PARA_SPLIT.split(path.read_text(encoding="utf-8")) if p.strip()])}


# ------------------------------------------------------------------ pdf


def extract_pdf(path: Path, min_words: int) -> tuple[list[dict], dict]:
    try:
        import pymupdf as fitz
    except ImportError as e:
        raise SystemExit("для чтения .pdf нужен pymupdf: pip install pymupdf") from e
    units, skipped = [], {}
    with fitz.open(str(path)) as pdf:
        for pno, page in enumerate(pdf, 1):
            for b in page.get_text("blocks"):
                if b[6] != 0:  # not a text block
                    continue
                text = re.sub(r"-\n(?=\w)", "", b[4])
                text = " ".join(text.split())
                if not text:
                    continue
                if _words(text) < min_words:
                    _count(skipped, "short")
                    continue
                if _content_skip_reason(text):
                    _count(skipped, _content_skip_reason(text))
                    continue
                units.append({"id": len(units), "page": pno, "text": text})
    if not units:
        raise SystemExit("в PDF нет текстового слоя (похоже на скан): очистить текст нельзя")
    return units, skipped


def rebuild_pdf(path: Path, units: list[dict], new_texts: list[str], out: Path) -> dict:
    from docx import Document

    doc = Document()
    for u, new in zip(units, new_texts):
        doc.add_paragraph(new if new.strip() else u["text"])
    doc.save(str(out))
    return {"note": "оформление PDF (шрифты, колонки, картинки, таблицы) не сохраняется: собран новый .docx только с текстом"}


def fingerprint_pdf(path: Path) -> dict:
    return fingerprint_docx(path)


# ------------------------------------------------------------------ dispatch

FORMATS = {
    ".docx": (extract_docx, rebuild_docx, fingerprint_docx),
    ".md": (extract_md, rebuild_md, fingerprint_md),
    ".markdown": (extract_md, rebuild_md, fingerprint_md),
    ".txt": (extract_txt, rebuild_txt, fingerprint_txt),
    ".pdf": (extract_pdf, rebuild_pdf, fingerprint_pdf),
}


def _fmt(path: Path):
    ext = path.suffix.lower()
    if ext not in FORMATS:
        raise SystemExit(f"формат {ext or '(без расширения)'} не поддерживается; поддерживаются: {', '.join(FORMATS)}")
    return FORMATS[ext]


def cmd_extract(a: argparse.Namespace) -> dict:
    inp, outdir = Path(a.input), Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    units, skipped = _fmt(inp)[0](inp, a.min_words)
    (outdir / "units.json").write_text(
        json.dumps({"source": inp.name, "units": units}, ensure_ascii=False, indent=1), encoding="utf-8")
    (outdir / "text.txt").write_text("\n\n".join(u["text"] for u in units), encoding="utf-8", newline="")
    return {
        "format": inp.suffix.lower(), "units": len(units), "words": sum(_words(u["text"]) for u in units),
        "skipped": skipped, "mixed_format": sum(1 for u in units if u.get("mixed_format")),
        "text_file": str(outdir / "text.txt"),
    }


def cmd_rebuild(a: argparse.Namespace) -> dict:
    inp, outdir, out = Path(a.input), Path(a.outdir), Path(a.output)
    units = json.loads((outdir / "units.json").read_text(encoding="utf-8"))["units"]
    cleaned = Path(a.cleaned).read_text(encoding="utf-8")
    new_texts = [p.strip() for p in PARA_SPLIT.split(cleaned.strip())] if cleaned.strip() else []
    if len(new_texts) != len(units):
        raise SystemExit(json.dumps({
            "ok": False,
            "error": f"число абзацев не совпало: было {len(units)}, стало {len(new_texts)}. "
                     "Перефразирование слило или разбило абзацы; очисти части заново по абзацам.",
        }, ensure_ascii=False))
    extra = _fmt(inp)[1](inp, units, new_texts, out)
    return {"ok": True, "output": str(out), "units_replaced": len(units), **extra}


def cmd_check(a: argparse.Namespace) -> dict:
    inp, out, outdir = Path(a.input), Path(a.output), Path(a.outdir)
    units = json.loads((outdir / "units.json").read_text(encoding="utf-8"))["units"]
    fp = _fmt(inp)[2]
    before, after = fp(inp), fp(out)
    issues: list[str] = []
    if inp.suffix.lower() == ".docx":
        for key in ("paragraphs", "tables", "pictures"):
            if before[key] != after[key]:
                issues.append(f"{key}: было {before[key]}, стало {after[key]}")
        if before["styles"] != after["styles"]:
            issues.append("стили абзацев изменились")
        touched = {u["index"] for u in units}
        changed_outside = [k for k, (x, y) in enumerate(zip(before["texts"], after["texts"]))
                           if k not in touched and x != y]
        if changed_outside:
            issues.append(f"изменились абзацы вне списка обработанных: {changed_outside[:10]}")
        if after["keep_marks"] > before["keep_marks"]:
            issues.append(
                f"добавлены отметки-квадратики (keepNext/keepLines/pageBreakBefore): "
                f"было {before['keep_marks']}, стало {after['keep_marks']}; их ставить нельзя"
            )
        return {"ok": not issues, "issues": issues, "keep_marks": after["keep_marks"]}
    elif inp.suffix.lower() in (".md", ".markdown"):
        for key in ("headings", "code_fences", "links"):
            if before[key] != after[key]:
                issues.append(f"{key} изменились")
        if before["emphasis"] != after["emphasis"]:
            issues.append("число ** или __ изменилось: проверь выделение")
    elif inp.suffix.lower() == ".txt":
        if before["paragraphs"] != after["paragraphs"]:
            issues.append(f"абзацев: было {before['paragraphs']}, стало {after['paragraphs']}")
    return {"ok": not issues, "issues": issues}


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("input"), e.add_argument("outdir")
    e.add_argument("--min-words", type=int, default=5)
    r = sub.add_parser("rebuild")
    r.add_argument("input"), r.add_argument("outdir"), r.add_argument("cleaned"), r.add_argument("output")
    c = sub.add_parser("check")
    c.add_argument("input"), c.add_argument("output"), c.add_argument("outdir")
    a = ap.parse_args(argv)
    res = {"extract": cmd_extract, "rebuild": cmd_rebuild, "check": cmd_check}[a.cmd](a)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
