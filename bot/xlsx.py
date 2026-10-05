"""Минимальный .xlsx без зависимостей (openpyxl потянул бы пересборку
Docker-образа на Маке): один лист, жирная шапка, фильтр и закреплённая
первая строка, цветные ячейки, ссылки через HYPERLINK — кликаются в Excel,
Numbers и Google Таблицах."""

import io
import zipfile
from dataclasses import dataclass
from xml.sax.saxutils import escape

# стили (cellXfs в styles.xml)
BOLD, GREEN, RED = 1, 2, 3


@dataclass
class Link:
    url: str
    text: str | None = None


@dataclass
class Styled:
    value: object
    style: int


def _col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref: str, value, style: int = 0) -> str:
    st = f' s="{style}"' if style else ""
    if isinstance(value, Styled):
        return _cell(ref, value.value, value.style)
    if value is None or value == "":
        return f'<c r="{ref}"{st}/>'
    if isinstance(value, bool):
        value = "да" if value else "нет"
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{st}><v>{value:g}</v></c>' if isinstance(value, float) \
            else f'<c r="{ref}"{st}><v>{value}</v></c>'
    if isinstance(value, Link):
        text = value.text or value.url
        url = value.url.replace('"', '""')[:255]
        label = text.replace('"', '""')[:255]
        formula = 'HYPERLINK("' + url + '","' + label + '")'
        return f'<c r="{ref}" t="str"{st}><f>{escape(formula)}</f><v>{escape(text)}</v></c>'
    return f'<c r="{ref}" t="inlineStr"{st}><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'


def make_xlsx(headers: list[str], rows: list[list], widths: list[int] | None = None,
              sheet: str = "Лист1") -> bytes:
    lines = []
    for r, values in enumerate([headers, *rows], 1):
        cells = "".join(_cell(f"{_col(c)}{r}", v, BOLD if r == 1 else 0)
                        for c, v in enumerate(values))
        lines.append(f'<row r="{r}">{cells}</row>')
    last = f"{_col(len(headers) - 1)}{len(rows) + 1}"
    cols = "".join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>'
                   for i, w in enumerate(widths or [], 1))
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
        'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        + (f"<cols>{cols}</cols>" if cols else "")
        + f'<sheetData>{"".join(lines)}</sheetData>'
        + f'<autoFilter ref="A1:{last}"/></worksheet>'
    )
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            "</Types>"),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{escape(sheet[:31])}" sheetId="1" r:id="rId1"/></sheets>'
            '<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">'
            f"'{escape(sheet[:31])}'!$A$1:${_col(len(headers) - 1)}${len(rows) + 1}</definedName></definedNames>"
            "</workbook>"),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            "</Relationships>"),
        "xl/styles.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
            '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="4"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFC6EFCE"/></patternFill></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FFFFC7CE"/></patternFill></fill></fills>'
            '<borders count="1"><border/></borders>'
            '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
            '<cellXfs count="4"><xf/>'
            '<xf fontId="1" applyFont="1"/>'
            '<xf fillId="2" applyFill="1"/>'
            '<xf fillId="3" applyFill="1"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            "</styleSheet>"),
        "xl/worksheets/sheet1.xml": sheet_xml,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()
