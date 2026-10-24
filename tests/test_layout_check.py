"""skills/remove-watermarks-ru/scripts/layout_check.py: логика проверки вёрстки (без Word)."""

from __future__ import annotations

import importlib.util
import os
import zipfile
from pathlib import Path

import pytest

docx = pytest.importorskip("docx")
pytest.importorskip("lxml")

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "remove-watermarks-ru"
    / "scripts"
    / "layout_check.py"
)
spec = importlib.util.spec_from_file_location("layout_check", SCRIPT)
lc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lc)


def _make(path: Path) -> None:
    d = docx.Document()
    d.add_paragraph("Обычный абзац текста перед разделом.")  # 0
    h = d.add_paragraph("Данные")  # 1: заголовок, прямой keepNext
    h.paragraph_format.keep_with_next = True
    d.add_paragraph("Текст раздела после заголовка.")  # 2
    cap = d.add_paragraph("Таблица 1")  # 3
    cap.paragraph_format.keep_with_next = True
    t = d.add_table(rows=2, cols=1)  # 4, 5
    for row in t.rows:
        row.cells[0].paragraphs[0].paragraph_format.keep_with_next = True
        row.cells[0].paragraphs[0].text = "ячейка"
    d.add_paragraph("Примечание под таблицей.")  # 6
    d.add_heading("Заголовок по стилю", level=1)  # 7: keepNext из стиля Heading 1
    d.add_paragraph("Последний абзац.")  # 8
    d.save(str(path))


def test_paragraph_info_reads_intent(tmp_path):
    src = tmp_path / "a.docx"
    _make(src)
    info = lc.paragraph_info(src)
    assert len(info) == 9
    assert [i for i, x in enumerate(info) if x["keep_next"]] == [1, 3, 4, 5, 7]
    assert info[4]["table"] == 0 and info[5]["table"] == 0 and info[6]["table"] is None
    assert info[1]["text"] == "Данные"


def test_issues_blocks_and_targets(tmp_path):
    src = tmp_path / "a.docx"
    _make(src)
    info = lc.paragraph_info(src)
    # заголовок «Данные» внизу стр. 1, таблица разорвана между стр. 2 и 3
    pages = [(1, 1, 5), (1, 1, 40), (2, 2, 1), (2, 2, 30), (2, 2, 32), (3, 3, 1), (3, 3, 2),
             (3, 3, 5), (3, 3, 6)]
    issues = lc.find_issues(info, pages)
    assert set(issues) == {("next", 1), ("next", 4)}
    assert "«Данные» осталось внизу стр. 1" in issues[("next", 1)]
    assert "таблица №1 по порядку разорвана: стр. 2 → 3" in issues[("next", 4)]
    # блок таблицы начинается с подписи: разрыв ставится в начало подписи
    assert lc.chain_start(info, 4) == 3
    assert lc.break_target(info, 3) == ("start", 3)
    assert lc.break_target(info, 1) == ("start", 1)


def test_break_before_table_without_caption_goes_to_previous_paragraph():
    info = [
        {"text": "a", "table": None, "keep_next": False, "keep_lines": False},
        {"text": "b", "table": 0, "keep_next": True, "keep_lines": False},
        {"text": "c", "table": 0, "keep_next": False, "keep_lines": False},
    ]
    assert lc.chain_start(info, 1) == 1
    assert lc.break_target(info, 1) == ("end", 0)
    info[1]["keep_next"] = info[2]["keep_next"] = True
    assert lc.break_target(info, 2) is None  # начинается внутри таблицы


def test_insert_page_breaks_only_touches_document(tmp_path):
    src, out = tmp_path / "a.docx", tmp_path / "b.docx"
    _make(src)
    lc.insert_page_breaks(src, out, {("start", 1), ("end", 0)})
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(out) as b:
        assert a.namelist() == b.namelist()
        for name in a.namelist():
            if name != "word/document.xml":
                assert a.read(name) == b.read(name)
    d = docx.Document(str(out))
    xml0 = d.paragraphs[0]._p.xml
    xml1 = d.paragraphs[1]._p.xml
    assert xml0.rstrip().endswith("</w:p>") and 'w:type="page"' in xml0
    assert xml1.index('w:type="page"') < xml1.index("Данные")
    assert len(lc.paragraph_info(out)) == 9


@pytest.mark.skipif(
    os.environ.get("WATERMARKS_TEST_WORD") != "1",
    reason="нужен Microsoft Word: WATERMARKS_TEST_WORD=1",
)
def test_word_layout_end_to_end(tmp_path, capsys):
    src = tmp_path / "a.docx"
    _make(src)
    assert lc.main(["check", str(src), str(src)]) == 0
