"""Проверка вёрстки .docx после снятия отметок-квадратиков и её исправление разрывами страниц.

Слой A снимает у абзацев «Не отрывать от следующего» и «Не разрывать абзац» (Word рисует их
чёрными квадратиками на полях). Без них заголовок может остаться внизу страницы, подпись
«Таблица N» оторваться от таблицы, а таблица разорваться. Скрипт берёт из ИСХОДНОГО файла, какие
абзацы автор держал вместе, раскладывает оба файла на страницы через Microsoft Word и сообщает
о новых разрывах (тех, которых не было в исходной вёрстке).

Команды (вывод всегда JSON в stdout):

  check ORIGINAL RESULT [--pdf OUT.pdf]
      Что разорвалось в RESULT по сравнению с ORIGINAL. --pdf дополнительно сохраняет RESULT в PDF.

  fix ORIGINAL RESULT OUT [--max-passes 12]
      Исправляет найденное явными разрывами страниц (квадратика они не дают) и пишет OUT.
      Переносит на новую страницу весь блок, который должен стоять вместе (заголовок с текстом,
      подпись с таблицей), сверху вниз, с повторной раскладкой после каждого разрыва. Блок,
      который не помещается на одну страницу, не трогает и перечисляет в unfixable.

Нужны Windows, установленный Microsoft Word и pywin32 (`python -m pip install pywin32`); без
них скрипт отвечает {"ok": false, "error": ...}, и вёрстку надо попросить пролистать вручную.
Число абзацев в ORIGINAL и RESULT должно совпадать (так собирает doc_text.py).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_OFF = {"0", "false", "off"}
_BOOKMARK_ID_BASE = 2_000_000
WD_ACTIVE_END_PAGE = 3  # wdActiveEndPageNumber
WD_FIRST_CHAR_LINE = 10  # wdFirstCharacterLineNumber
WD_STATISTIC_PAGES = 2
WD_EXPORT_PDF = 17


def q(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _on(el: Any) -> bool | None:
    if el is None:
        return None
    val = el.get(q("val"))
    return val is None or val.strip().lower() not in _OFF


def _xml(path: Path, name: str) -> Any:
    from lxml import etree

    with zipfile.ZipFile(path) as zf:
        if name not in zf.namelist():
            return None
        return etree.fromstring(zf.read(name))


def main_story_paragraphs(root: Any) -> list[Any]:
    """Абзацы основного текста по порядку (включая таблицы, без надписей и запасных копий фигур)."""
    from lxml import etree

    out = []
    for p in root.iter(q("p")):
        anc = p.getparent()
        while anc is not None:
            if etree.QName(anc).localname in ("txbxContent", "Fallback"):
                break
            anc = anc.getparent()
        else:
            out.append(p)
    return out


def _style_flags(styles: Any) -> tuple[dict[str, dict[str, bool | None]], dict[str, str], str]:
    own: dict[str, dict[str, bool | None]] = {}
    parent: dict[str, str] = {}
    default = ""
    if styles is None:
        return own, parent, default
    for st in styles.iter(q("style")):
        sid = st.get(q("styleId")) or ""
        ppr = st.find(q("pPr"))
        own[sid] = {
            f: _on(ppr.find(q(f)) if ppr is not None else None) for f in ("keepNext", "keepLines")
        }
        based = st.find(q("basedOn"))
        if based is not None:
            parent[sid] = based.get(q("val")) or ""
        if st.get(q("type")) == "paragraph" and st.get(q("default")) in ("1", "true", "on"):
            default = sid
    return own, parent, default


def _effective(p: Any, flag: str, styles: tuple) -> bool:
    own, parent, default = styles
    ppr = p.find(q("pPr"))
    if ppr is not None:
        direct = _on(ppr.find(q(flag)))
        if direct is not None:
            return direct
    sid = default
    if ppr is not None and ppr.find(q("pStyle")) is not None:
        sid = ppr.find(q("pStyle")).get(q("val")) or default
    seen: set[str] = set()
    while sid in own and sid not in seen:
        seen.add(sid)
        if own[sid].get(flag) is not None:
            return bool(own[sid][flag])
        sid = parent.get(sid, "")
    return False


def paragraph_info(path: Path) -> list[dict]:
    """Для каждого абзаца: начало текста, номер таблицы (по порядку) и флаги keepNext/keepLines."""
    root = _xml(path, "word/document.xml")
    styles = _style_flags(_xml(path, "word/styles.xml"))
    # Keys hold the lxml proxies alive, so getparent() returns the same objects below.
    top_tables: dict[Any, int] = {}
    for tbl in root.iter(q("tbl")):
        anc, nested = tbl.getparent(), False
        while anc is not None:
            if anc.tag == q("tbl"):
                nested = True
                break
            anc = anc.getparent()
        if not nested:
            top_tables[tbl] = len(top_tables)
    info = []
    for p in main_story_paragraphs(root):
        table = None
        anc = p.getparent()
        while anc is not None:
            if anc.tag == q("tbl") and anc in top_tables:
                table = top_tables[anc]
            anc = anc.getparent()
        text = "".join(t.text or "" for t in p.iter(q("t"))).strip()
        if not text and next(p.iter(q("drawing"), q("pict")), None) is not None:
            text = "(рисунок)"
        elif not text and next(p.iter("{*}oMath"), None) is not None:
            text = "(формула)"
        info.append({
            "text": text[:60] + ("…" if len(text) > 60 else ""),
            "table": table,
            "keep_next": _effective(p, "keepNext", styles),
            "keep_lines": table is None and _effective(p, "keepLines", styles),
        })
    return info


# ------------------------------------------------------------------ правка XML


def _rewrite_document(src: Path, dest: Path, mutate) -> None:
    """Копия src с изменённым word/document.xml; остальные части архива байт в байт."""
    from lxml import etree

    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                root = etree.fromstring(data)
                mutate(root)
                data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            zout.writestr(item, data)


def _page_break_run() -> Any:
    from lxml import etree

    r = etree.Element(q("r"))
    br = etree.SubElement(r, q("br"))
    br.set(q("type"), "page")
    return r


def insert_page_breaks(src: Path, dest: Path, targets: set[tuple[str, int]]) -> None:
    """targets: ("start", i) — разрыв в начале абзаца i, ("end", i) — в конце (перед таблицей)."""

    def mutate(root: Any) -> None:
        pars = main_story_paragraphs(root)
        for where, i in targets:
            p = pars[i]
            if where == "start":
                ppr = p.find(q("pPr"))
                p.insert(1 if ppr is not None and p[0] is ppr else 0, _page_break_run())
            else:
                p.append(_page_break_run())

    _rewrite_document(src, dest, mutate)


def _add_bookmarks(src: Path, dest: Path) -> int:
    """Копия с закладками lcS{i} (начало абзаца) и lcE{i} (конец) для чтения страниц из Word."""
    from lxml import etree

    count = [0]

    def mutate(root: Any) -> None:
        pars = main_story_paragraphs(root)
        count[0] = len(pars)
        for i, p in enumerate(pars):
            ppr = p.find(q("pPr"))
            at = 1 if ppr is not None and len(p) and p[0] is ppr else 0
            for k, (name, pos) in enumerate(((f"lcS{i}", at), (f"lcE{i}", None))):
                bid = str(_BOOKMARK_ID_BASE + 2 * i + k)
                bs = etree.Element(q("bookmarkStart"))
                bs.set(q("id"), bid)
                bs.set(q("name"), name)
                be = etree.Element(q("bookmarkEnd"))
                be.set(q("id"), bid)
                if pos is None:
                    p.append(bs)
                    p.append(be)
                else:
                    p.insert(pos, bs)
                    p.insert(pos + 1, be)

    _rewrite_document(src, dest, mutate)
    return count[0]


# ------------------------------------------------------------------ Word


class WordLayout:
    """Раскладка на страницы средствами Microsoft Word (один экземпляр Word на все прогоны)."""

    def __init__(self) -> None:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        self.app = win32com.client.DispatchEx("Word.Application")
        self.app.Visible = False
        self.app.DisplayAlerts = 0
        self.tmp = Path(tempfile.mkdtemp(prefix="layout-check-"))

    def pages(self, path: Path, pdf: Path | None = None) -> tuple[list[tuple[int, int, int]], int]:
        """[(стр. начала, стр. конца, строка начала на странице)] по абзацам и число страниц."""
        marked = self.tmp / f"m{len(list(self.tmp.iterdir()))}.docx"
        n = _add_bookmarks(path, marked)
        doc = self.app.Documents.Open(
            FileName=str(marked), ConfirmConversions=False, ReadOnly=True,
            AddToRecentFiles=False, Visible=False,
        )
        try:
            doc.Repaginate()
            out = []
            for i in range(n):
                start = doc.Bookmarks(f"lcS{i}").Range
                end = doc.Bookmarks(f"lcE{i}").Range
                out.append((
                    int(start.Information(WD_ACTIVE_END_PAGE)),
                    int(end.Information(WD_ACTIVE_END_PAGE)),
                    int(start.Information(WD_FIRST_CHAR_LINE)),
                ))
            total = int(doc.ComputeStatistics(WD_STATISTIC_PAGES))
            if pdf is not None:
                # Word cannot export to long paths: write into the short temp dir, then copy.
                tmp_pdf = self.tmp / "out.pdf"
                doc.ExportAsFixedFormat(OutputFileName=str(tmp_pdf), ExportFormat=WD_EXPORT_PDF)
                shutil.copyfile(tmp_pdf, pdf)
        finally:
            start = end = None
            doc.Close(SaveChanges=0)
            doc = None
        return out, total

    def close(self) -> None:
        import gc

        import pythoncom

        try:
            self.app.Quit()
        finally:
            # Drop every COM proxy before uninitializing, otherwise releasing them after
            # Word exits raises RPC_E_DISCONNECTED (0x800706be) noise.
            self.app = None
            gc.collect()
            pythoncom.CoUninitialize()
            shutil.rmtree(self.tmp, ignore_errors=True)


# ------------------------------------------------------------------ проверка


def find_issues(info: list[dict], pages: list[tuple[int, int, int]]) -> dict[tuple[str, int], str]:
    """Разрывы там, где автор держал абзацы вместе: ключ (вид, абзац) -> описание."""
    issues: dict[tuple[str, int], str] = {}
    for i, inf in enumerate(info):
        if inf["keep_next"] and i + 1 < len(info) and pages[i][1] < pages[i + 1][0]:
            issues[("next", i)] = describe(info, pages, i)
        if inf["keep_lines"] and pages[i][0] < pages[i][1]:
            issues[("lines", i)] = (
                f"абзац «{inf['text'] or '(пустая строка)'}» разорван между стр. "
                f"{pages[i][0]} и {pages[i][1]}"
            )
    return issues


def describe(info: list[dict], pages: list[tuple[int, int, int]], i: int) -> str:
    a, b = info[i], info[i + 1]
    pa, pb = pages[i][1], pages[i + 1][0]
    ta, tb = a["table"], b["table"]
    ta_text = a["text"] or "(пустая строка)"
    tb_text = b["text"] or "(пустая строка)"
    if ta is not None and ta == tb:
        return f"таблица №{ta + 1} по порядку разорвана: стр. {pa} → {pb} (строка «{tb_text}»)"
    if tb is not None:
        return f"«{ta_text}» на стр. {pa}, а таблица №{tb + 1} по порядку начинается на стр. {pb}"
    if ta is not None:
        return (
            f"таблица №{ta + 1} по порядку кончается на стр. {pa}, а «{tb_text}» уже на стр. {pb}"
        )
    return f"«{ta_text}» осталось внизу стр. {pa}, а «{tb_text}» уже на стр. {pb}"


def chain_start(info: list[dict], i: int) -> int:
    """Первый абзац блока, который автор держал вместе с абзацем i."""
    while i > 0 and info[i - 1]["keep_next"]:
        i -= 1
    return i


def break_target(info: list[dict], s: int) -> tuple[str, int] | None:
    """Куда ставить разрыв, чтобы блок с началом s ушёл на новую страницу (None — некуда)."""
    if info[s]["table"] is None:
        return ("start", s)
    first = s
    while first > 0 and info[first - 1]["table"] == info[s]["table"]:
        first -= 1
    if first != s or first == 0 or info[first - 1]["table"] is not None:
        return None  # блок начинается внутри таблицы или таблица идёт сразу после другой
    return ("end", first - 1)


def _load(original: Path, result: Path) -> list[dict]:
    info = paragraph_info(original)
    n_result = len(main_story_paragraphs(_xml(result, "word/document.xml")))
    if n_result != len(info):
        raise ValueError(
            f"число абзацев не совпадает: в исходном {len(info)}, в результате {n_result}"
        )
    return info


def cmd_check(a: argparse.Namespace) -> dict:
    original, result = Path(a.original), Path(a.result)
    info = _load(original, result)
    word = WordLayout()
    try:
        pages_before, total_before = word.pages(original)
        pages_after, total_after = word.pages(result, Path(a.pdf) if a.pdf else None)
    finally:
        word.close()
    before = find_issues(info, pages_before)
    new = {k: v for k, v in find_issues(info, pages_after).items() if k not in before}
    blocks: dict[int, list[str]] = {}
    for (_kind, i), text in sorted(new.items(), key=lambda kv: kv[0][1]):
        blocks.setdefault(chain_start(info, i), []).append(text)
    return {
        "ok": not blocks,
        "pages_before": total_before,
        "pages_after": total_after,
        "issues": [" ; ".join(v) for v in blocks.values()],
        **({"pdf": str(Path(a.pdf).resolve())} if a.pdf else {}),
    }


def cmd_fix(a: argparse.Namespace) -> dict:
    original, result, out = Path(a.original), Path(a.result), Path(a.output)
    info = _load(original, result)
    word = WordLayout()
    targets: set[tuple[str, int]] = set()
    by_block: dict[int, tuple[str, int]] = {}
    unfixable: dict[int, str] = {}
    fixed: list[str] = []
    passes = 0
    try:
        before = find_issues(info, word.pages(original)[0])
        work = word.tmp / "work.docx"
        shutil.copyfile(result, work)
        while passes < a.max_passes:
            passes += 1
            pages, total = word.pages(work)
            new = {k: v for k, v in find_issues(info, pages).items() if k not in before}
            todo = sorted(
                ((chain_start(info, i), text) for (_k, i), text in new.items()),
                key=lambda st: st[0],
            )
            todo = [(s, t) for s, t in todo if s not in unfixable]
            if not todo:
                break
            s, text = todo[0]
            target = break_target(info, s)
            if s in by_block:  # разрыв уже стоит, а блок всё равно разорван: не помещается
                targets.discard(by_block.pop(s))
                fixed[:] = [f for f in fixed if not f.endswith(f"#{s}")]
                unfixable[s] = f"{text} (блок не помещается на одну страницу)"
            elif target is None:
                unfixable[s] = f"{text} (некуда поставить разрыв: блок начинается внутри таблицы)"
            else:
                first_para = target[1] + 1 if target[0] == "end" else target[1]
                if pages[first_para][2] == 1:  # блок и так начинается с новой страницы
                    unfixable[s] = f"{text} (блок не помещается на одну страницу)"
                else:
                    targets.add(target)
                    by_block[s] = target
                    fixed.append(f"{text}#{s}")
            insert_page_breaks(result, work, targets)
        shutil.copyfile(work, out)
        final = {k for k in find_issues(info, word.pages(out)[0]) if k not in before}
    finally:
        word.close()
    return {
        "ok": True,
        "output": str(out),
        "page_breaks_inserted": len(targets),
        "fixed": [f.rsplit("#", 1)[0] for f in fixed],
        "unfixable": list(unfixable.values()),
        "remaining_issues": len(final),
        "passes": passes,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("original")
    c.add_argument("result")
    c.add_argument("--pdf")
    f = sub.add_parser("fix")
    f.add_argument("original")
    f.add_argument("result")
    f.add_argument("output")
    f.add_argument("--max-passes", type=int, default=12)
    a = ap.parse_args(argv)
    try:
        res = cmd_check(a) if a.cmd == "check" else cmd_fix(a)
    except ImportError as exc:
        res = {"ok": False, "error": f"нет pywin32/lxml ({exc}); вёрстку проверить нельзя"}
    except Exception as exc:  # noqa: BLE001 - Word/COM errors are reported, not raised
        res = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
