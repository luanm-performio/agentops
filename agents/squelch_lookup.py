from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import cast
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from tools.db_config import load_config

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "tools" / "config.yaml"


@dataclass(frozen=True)
class SquelchQueryResult:
    columns: list[str]
    rows: list[dict[str, object]]

    @property
    def row_count(self) -> int:
        return len(self.rows)


def get_squelch_regions() -> list[str]:
    regions = load_config(str(_CONFIG_PATH))
    return [region.name for region in regions if region.name]


def run_squelch_query(
    host_name: str,
    query: str,
    regions: list[str],
) -> SquelchQueryResult:
    from tools.squelch import run_squelch  # noqa: PLC0415

    output = run_squelch(host_name=host_name, query=query, regions=regions)
    return normalize_squelch_output(output)


def normalize_squelch_output(output: object) -> SquelchQueryResult:
    if isinstance(output, SquelchQueryResult):
        return output

    if isinstance(output, list):
        rows = _normalize_mapping_rows(output)
        columns = _columns_for_rows(rows)
        ordered_rows = [
            {column: row.get(column, "") for column in columns} for row in rows
        ]
        return SquelchQueryResult(columns=columns, rows=ordered_rows)

    raise TypeError("squelch.py must return a list of dictionaries.")


def _normalize_mapping_rows(rows: list[object]) -> list[dict[str, object]]:
    normalized_rows: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise TypeError("squelch.py rows must be dictionaries.")
        normalized_rows.append(
            {
                str(key): value
                for key, value in cast(Mapping[object, object], row).items()
            }
        )
    return normalized_rows


def _columns_for_rows(rows: Sequence[Mapping[str, object]]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return columns


def build_squelch_xlsx(result: SquelchQueryResult) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as workbook:
        workbook.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        )
        workbook.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        workbook.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Squelch" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>""",
        )
        workbook.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>""",
        )
        workbook.writestr("xl/worksheets/sheet1.xml", _worksheet_xml(result))
    return buffer.getvalue()


def _worksheet_xml(result: SquelchQueryResult) -> str:
    rows = [result.columns]
    rows.extend(
        [_cell_value(row.get(column)) for column in result.columns]
        for row in result.rows
    )
    row_xml = "\n".join(
        _row_xml(row_number, values) for row_number, values in enumerate(rows, start=1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
{row_xml}
  </sheetData>
</worksheet>"""


def _row_xml(row_number: int, values: Sequence[str]) -> str:
    cells = "".join(
        _cell_xml(row_number, column_number, value)
        for column_number, value in enumerate(values, start=1)
    )
    return f'    <row r="{row_number}">{cells}</row>'


def _cell_xml(row_number: int, column_number: int, value: str) -> str:
    cell_ref = f"{_column_name(column_number)}{row_number}"
    return f'<c r="{cell_ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'


def _cell_value(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _column_name(column_number: int) -> str:
    name = ""
    current = column_number
    while current:
        current, remainder = divmod(current - 1, 26)
        name = chr(65 + remainder) + name
    return name
