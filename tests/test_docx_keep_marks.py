"""DOCX: paragraph keep marks (the black squares Word shows in the margin) are removed."""

from __future__ import annotations

import io
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "service" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from container_meta import clean_container, clean_docx  # noqa: E402

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _docx(body: str, styles: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/></Types>',
        )
        zf.writestr(
            "word/document.xml",
            f'<?xml version="1.0"?><w:document {W}><w:body>{body}</w:body></w:document>',
        )
        if styles is not None:
            zf.writestr(
                "word/styles.xml", f'<?xml version="1.0"?><w:styles {W}>{styles}</w:styles>'
            )
    return buf.getvalue()


def _part(data: bytes, name: str = "word/document.xml") -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read(name).decode("utf-8")


def _texts(xml: str) -> list[str]:
    return re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml)


def test_keep_next_and_keep_lines_removed_text_untouched():
    body = (
        "<w:p><w:pPr><w:keepNext/><w:keepLines/><w:jc w:val=\"center\"/></w:pPr>"
        "<w:r><w:t>Таблица 1</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:pPr><w:keepNext w:val=\"1\"/></w:pPr>"
        "<w:r><w:t>DOL</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "<w:p><w:pPr><w:keepNext w:val='true'></w:keepNext></w:pPr>"
        "<w:r><w:t>Данные</w:t></w:r></w:p>"
    )
    data = _docx(body)
    cleaned, actions = clean_docx(data)
    out = _part(cleaned)
    assert "keepNext" not in out and "keepLines" not in out
    assert '<w:jc w:val="center"/>' in out
    assert _texts(out) == _texts(_part(data))
    assert any("keepNext=3 keepLines=1" in a for a in actions)


def test_explicit_off_flags_are_left_alone():
    body = '<w:p><w:pPr><w:keepNext w:val="0"/></w:pPr><w:r><w:t>x</w:t></w:r></w:p>'
    cleaned, actions = clean_docx(_docx(body))
    assert '<w:keepNext w:val="0"/>' in _part(cleaned)
    assert not any("keep marks" in a for a in actions)


def test_direct_page_break_before_becomes_page_break_run():
    body = (
        "<w:p><w:r><w:t>first</w:t></w:r></w:p>"
        "<w:p><w:pPr><w:pageBreakBefore/></w:pPr><w:r><w:t>second</w:t></w:r></w:p>"
    )
    cleaned, actions = clean_docx(_docx(body))
    out = _part(cleaned)
    assert "pageBreakBefore" not in out
    assert '<w:pPr></w:pPr><w:r><w:br w:type="page"/></w:r><w:r><w:t>second</w:t>' in out
    assert out.count('w:type="page"') == 1
    assert any("page breaks inserted=1" in a for a in actions)


def test_style_page_break_before_moves_to_paragraphs_incl_based_on():
    styles = (
        '<w:style w:type="paragraph" w:styleId="Chapter"><w:pPr><w:pageBreakBefore/>'
        "<w:keepNext/></w:pPr></w:style>"
        '<w:style w:type="paragraph" w:styleId="Appendix"><w:basedOn w:val="Chapter"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="NoBreak"><w:basedOn w:val="Chapter"/>'
        '<w:pPr><w:pageBreakBefore w:val="0"/></w:pPr></w:style>'
    )
    body = (
        '<w:p><w:pPr><w:pStyle w:val="Chapter"/></w:pPr><w:r><w:t>A</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="Appendix"/></w:pPr><w:r><w:t>B</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="NoBreak"/></w:pPr><w:r><w:t>C</w:t></w:r></w:p>'
        '<w:p><w:pPr><w:pStyle w:val="Chapter"/><w:pageBreakBefore w:val="0"/></w:pPr>'
        "<w:r><w:t>D</w:t></w:r></w:p>"
    )
    cleaned, _ = clean_docx(_docx(body, styles))
    out = _part(cleaned)
    st = _part(cleaned, "word/styles.xml")
    assert "keepNext" not in st
    assert '<w:pageBreakBefore/>' not in st
    breaks = [m.start() for m in re.finditer('w:type="page"', out)]
    assert len(breaks) == 2
    assert breaks[0] < out.index(">A<") and out.index(">A<") < breaks[1] < out.index(">B<")


def test_no_page_break_inside_table_cells():
    body = (
        "<w:tbl><w:tr><w:tc><w:p><w:pPr><w:pageBreakBefore/></w:pPr>"
        "<w:r><w:t>cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    )
    cleaned, _ = clean_docx(_docx(body))
    out = _part(cleaned)
    assert "pageBreakBefore" not in out and 'w:type="page"' not in out


def test_tracked_change_history_does_not_trigger_break():
    body = (
        "<w:p><w:pPr><w:pPrChange w:id=\"1\" w:author=\"a\"><w:pPr><w:pageBreakBefore/></w:pPr>"
        "</w:pPrChange></w:pPr><w:r><w:t>x</w:t></w:r></w:p>"
    )
    cleaned, _ = clean_docx(_docx(body))
    out = _part(cleaned)
    assert 'w:type="page"' not in out
    assert "pageBreakBefore" not in out


def test_option_off_keeps_marks(tmp_path: Path):
    body = "<w:p><w:pPr><w:keepNext/></w:pPr><w:r><w:t>x</w:t></w:r></w:p>"
    src = tmp_path / "in.docx"
    src.write_bytes(_docx(body))
    dest = tmp_path / "out.docx"
    clean_container(src, dest, strip_keep_marks=False)
    assert "<w:keepNext/>" in _part(dest.read_bytes())
    clean_container(src, dest)
    assert "keepNext" not in _part(dest.read_bytes())


def test_server_accepts_strip_keep_marks_option():
    import server

    assert server.ALLOWED_CLEAN_OPTIONS["strip_keep_marks"] is bool
