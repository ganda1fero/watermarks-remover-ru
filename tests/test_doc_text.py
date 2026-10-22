"""skills/remove-watermarks-ru/scripts/doc_text.py: разбор файла на абзацы и сборка обратно."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

docx = pytest.importorskip("docx")

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "remove-watermarks-ru" / "scripts" / "doc_text.py"
spec = importlib.util.spec_from_file_location("doc_text", SCRIPT)
doc_text = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doc_text)

P1 = "Первый абзац нашего документа достаточно длинный для обработки моделью."
P2 = "Второй абзац лежит внутри ячейки таблицы и тоже должен быть обработан."
P3 = "Третий абзац содержит жирное слово и обычный текст рядом друг с другом."


def _run(argv):
    return doc_text.main(argv)


def _make_docx(path: Path) -> None:
    d = docx.Document()
    d.add_heading("Заголовок документа", level=1)
    d.add_paragraph(P1)
    d.add_paragraph("Коротко.")
    par = d.add_paragraph()
    par.add_run("Третий абзац содержит ")
    par.add_run("жирное слово").bold = True
    par.add_run(" и обычный текст рядом друг с другом.")
    t = d.add_table(rows=1, cols=1)
    t.cell(0, 0).text = P2
    d.save(str(path))


def test_docx_roundtrip_keeps_structure(tmp_path, capsys):
    src, work, out = tmp_path / "in.docx", tmp_path / "work", tmp_path / "out.docx"
    _make_docx(src)
    assert _run(["extract", str(src), str(work)]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["units"] == 3 and info["mixed_format"] == 1
    assert info["skipped"].get("heading_or_code_style") == 1 and info["skipped"].get("short") == 1
    text = (work / "text.txt").read_text(encoding="utf-8").split("\n\n")
    assert text == [P1, P3, P2] or sorted(text) == sorted([P1, P2, P3])
    cleaned = tmp_path / "cleaned.txt"
    cleaned.write_text("\n\n".join(t.replace("абзац", "фрагмент") for t in text), encoding="utf-8")
    assert _run(["rebuild", str(src), str(work), str(cleaned), str(out)]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["ok"] and res["mixed_format_paragraphs_flattened"] == 1
    assert _run(["check", str(src), str(out), str(work)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    after = docx.Document(str(out))
    texts = [p.text for p in after.paragraphs] + [c.text for c in after.tables[0]._cells]
    assert any("Первый фрагмент" in t for t in texts) and "Заголовок документа" in texts and "Коротко." in texts
    assert len(after.tables) == 1


def test_rebuild_refuses_when_paragraph_count_changes(tmp_path, capsys):
    src, work = tmp_path / "in.docx", tmp_path / "work"
    _make_docx(src)
    _run(["extract", str(src), str(work)])
    capsys.readouterr()
    bad = tmp_path / "bad.txt"
    bad.write_text("только один абзац", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        _run(["rebuild", str(src), str(work), str(bad), str(tmp_path / "o.docx")])
    assert "число абзацев не совпало" in str(e.value)


def test_docx_skips_hyperlink_paragraphs(tmp_path, capsys):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    src, work = tmp_path / "in.docx", tmp_path / "work"
    d = docx.Document()
    par = d.add_paragraph(P1)
    par._p.append(OxmlElement("w:hyperlink"))
    d.add_paragraph(P2)
    d.save(str(src))
    assert qn("w:hyperlink")
    _run(["extract", str(src), str(work)])
    info = json.loads(capsys.readouterr().out)
    assert info["units"] == 1 and info["skipped"] == {"hyperlink": 1}


MD = """---
title: Пример
---

# Заголовок

Это обычный абзац прозы,
который перенесён на две строки.

```python
code = 1
```

Абзац со [ссылкой](http://x.y) на сайт и ещё словами для длины.

- Первый пункт списка достаточно длинный для обработки.
- Второй пункт списка тоже достаточно длинный для обработки.
"""


def test_md_roundtrip_protects_markup(tmp_path, capsys):
    src, work, out = tmp_path / "in.md", tmp_path / "work", tmp_path / "out.md"
    src.write_text(MD, encoding="utf-8")
    _run(["extract", str(src), str(work)])
    info = json.loads(capsys.readouterr().out)
    assert info["units"] == 3
    assert info["skipped"]["markup_inside"] == 1 and info["skipped"]["code_block"] == 1
    assert info["skipped"]["front_matter"] == 1 and info["skipped"]["heading"] == 1
    paras = (work / "text.txt").read_text(encoding="utf-8").split("\n\n")
    cleaned = tmp_path / "c.txt"
    cleaned.write_text("\n\n".join(p.upper() for p in paras), encoding="utf-8")
    assert _run(["rebuild", str(src), str(work), str(cleaned), str(out)]) == 0
    capsys.readouterr()
    res = out.read_text(encoding="utf-8")
    assert "# Заголовок" in res and "code = 1" in res and "[ссылкой](http://x.y)" in res
    assert "ЭТО ОБЫЧНЫЙ АБЗАЦ ПРОЗЫ, КОТОРЫЙ ПЕРЕНЕСЁН НА ДВЕ СТРОКИ." in res
    assert "- ПЕРВЫЙ ПУНКТ СПИСКА" in res and "- ВТОРОЙ ПУНКТ СПИСКА" in res
    assert _run(["check", str(src), str(out), str(work)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_txt_roundtrip_keeps_separators(tmp_path, capsys):
    src, work, out = tmp_path / "in.txt", tmp_path / "work", tmp_path / "out.txt"
    src.write_text(P1 + "\n\n\n" + "Мало слов." + "\n\n" + P2, encoding="utf-8")
    _run(["extract", str(src), str(work)])
    assert json.loads(capsys.readouterr().out)["units"] == 2
    c = tmp_path / "c.txt"
    c.write_text("А один\n\nБ два", encoding="utf-8")
    _run(["rebuild", str(src), str(work), str(c), str(out)])
    assert out.read_text(encoding="utf-8") == "А один\n\n\nМало слов.\n\nБ два"


def test_unsupported_format(tmp_path):
    f = tmp_path / "x.rtf"
    f.write_text("x")
    with pytest.raises(SystemExit) as e:
        _run(["extract", str(f), str(tmp_path / "w")])
    assert "не поддерживается" in str(e.value)


def test_docx_skips_paragraphs_with_inline_formulas(tmp_path, capsys):
    from docx.oxml import parse_xml

    src, work = tmp_path / "f.docx", tmp_path / "work"
    d = docx.Document()
    par = d.add_paragraph("Здесь формула вставлена внутри предложения и должна остаться на своём месте в тексте.")
    par._p.append(parse_xml(
        '<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">'
        '<m:r><m:t>x</m:t></m:r></m:oMath>'
    ))
    d.add_paragraph(P1)
    d.save(str(src))
    _run(["extract", str(src), str(work)])
    info = json.loads(capsys.readouterr().out)
    assert info["units"] == 1 and info["skipped"] == {"formula": 1}


def test_bibliography_and_foreign_text_are_never_rewritten(tmp_path, capsys):
    src, work = tmp_path / "b.docx", tmp_path / "work"
    d = docx.Document()
    d.add_paragraph(P1)
    d.add_paragraph("Sims, C.A. Macroeconomics and Reality / C.A. Sims // Econometrica. \u2013 1980. \u2013 Vol. 48, \u2116 1. \u2013 P. 1\u201348.")
    d.add_paragraph("\u0411\u0435\u0437\u0431\u043e\u0440\u043e\u0434\u043e\u0432\u0430, \u0410. \u0410\u043d\u0430\u043b\u0438\u0437 \u043c\u0435\u0445\u0430\u043d\u0438\u0437\u043c\u0430 / \u0410. \u0411\u0435\u0437\u0431\u043e\u0440\u043e\u0434\u043e\u0432\u0430 // \u041a\u0432\u0430\u043d\u0442\u0438\u043b\u044c. \u2013 2015. \u2013 \u2116 13. \u2013 \u0421. 41\u201361 \u0438 \u0435\u0449\u0451 \u0441\u043b\u043e\u0432.")
    d.add_paragraph("The article examines how deposit dollarization affects the transmission of monetary policy shocks.")
    d.save(str(src))
    _run(["extract", str(src), str(work)])
    info = json.loads(capsys.readouterr().out)
    assert info["units"] == 1
    assert info["skipped"] == {"bibliography": 1, "non_russian": 2}  # the English entry counts as foreign text


def test_unchanged_paragraphs_keep_their_formatting(tmp_path, capsys):
    src, work, out = tmp_path / "u.docx", tmp_path / "work", tmp_path / "out.docx"
    _make_docx(src)
    _run(["extract", str(src), str(work)])
    capsys.readouterr()
    cleaned = tmp_path / "same.txt"
    cleaned.write_text((work / "text.txt").read_text(encoding="utf-8"), encoding="utf-8")  # nothing changed
    _run(["rebuild", str(src), str(work), str(cleaned), str(out)])
    res = json.loads(capsys.readouterr().out)
    assert res["mixed_format_paragraphs_flattened"] == 0  # the bold run in the third paragraph is intact
    bold = [r.text for p in docx.Document(str(out)).paragraphs for r in p.runs if r.bold]
    assert "жирное слово" in bold
