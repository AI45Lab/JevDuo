"""Deterministic fixture generators for case suites."""

from __future__ import annotations

import csv
import html
import io
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from pydantic import BaseModel

from northcua.experiments.cases.contracts import (
    FixtureGeneratorId,
    GeneratedFixtureSpec,
    HtmlSimplePageParams,
    ImagePpmParams,
    SetupScriptParams,
    SpreadsheetRowsParams,
    TextFileParams,
)


class FixtureGenerator(ABC):
    """Generate one fixture file from typed generator metadata."""

    generator_id: FixtureGeneratorId

    @abstractmethod
    def write(self, spec: GeneratedFixtureSpec, path: Path, *, task_id: str) -> None:
        """Write fixture contents to ``path``.

        Args:
            spec: Typed generator spec containing the generator id and params.
            path: Local file path to create.
            task_id: Case id used by generators that stamp deterministic text.
        """


class HtmlSimplePageGenerator(FixtureGenerator):
    """Generate a small deterministic HTML document for browser cases."""

    generator_id = FixtureGeneratorId.HTML_SIMPLE_PAGE

    def write(self, spec: GeneratedFixtureSpec, path: Path, *, task_id: str) -> None:
        """Write a deterministic HTML page fixture.

        Args:
            spec: HTML generator spec with title, heading, and body params.
            path: Local HTML file path to create.
            task_id: Case id used as the fallback heading.
        """

        params = _params(spec, HtmlSimplePageParams)
        title = params.title
        heading = params.heading or f"NorthCUA Case {task_id}"
        body = params.body
        path.write_text(
            "\n".join(
                [
                    "<!doctype html>",
                    "<html><head>",
                    '<meta charset="utf-8">',
                    f"<title>{html.escape(title)}</title>",
                    "</head><body>",
                    f"<h1>{html.escape(heading)}</h1>",
                    f"<p>{html.escape(body)}</p>",
                    "</body></html>",
                ]
            ),
            encoding="utf-8",
        )


class TextFileGenerator(FixtureGenerator):
    """Generate a deterministic UTF-8 text file for editor cases."""

    generator_id = FixtureGeneratorId.TEXT_FILE

    def write(self, spec: GeneratedFixtureSpec, path: Path, *, task_id: str) -> None:
        """Write a deterministic text fixture.

        Args:
            spec: Text generator spec with UTF-8 text content.
            path: Local text file path to create.
            task_id: Unused case id accepted for the shared generator API.
        """

        _ = task_id
        path.write_text(_params(spec, TextFileParams).text, encoding="utf-8")


class SetupScriptGenerator(FixtureGenerator):
    """Generate an allowlisted bash setup script from typed commands."""

    generator_id = FixtureGeneratorId.SETUP_SCRIPT

    def write(self, spec: GeneratedFixtureSpec, path: Path, *, task_id: str) -> None:
        """Write a deterministic setup script fixture.

        Args:
            spec: Setup-script generator spec with validated shell commands.
            path: Local script path to create.
            task_id: Unused case id accepted for the shared generator API.
        """

        _ = task_id
        params = _params(spec, SetupScriptParams)
        path.write_text(
            "\n".join(["#!/usr/bin/env bash", "set -euo pipefail", *params.commands, ""]),
            encoding="utf-8",
        )


class SpreadsheetCsvGenerator(FixtureGenerator):
    """Generate a deterministic CSV spreadsheet fixture."""

    generator_id = FixtureGeneratorId.SPREADSHEET_CSV

    def write(self, spec: GeneratedFixtureSpec, path: Path, *, task_id: str) -> None:
        """Write a deterministic CSV spreadsheet.

        Args:
            spec: Spreadsheet generator spec with validated rows.
            path: Local CSV file path to create.
            task_id: Unused case id accepted for the shared generator API.
        """

        _ = task_id
        rows = _rows_from_params(_params(spec, SpreadsheetRowsParams))
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerows(rows)


class SpreadsheetXlsxGenerator(FixtureGenerator):
    """Generate a minimal deterministic XLSX workbook fixture."""

    generator_id = FixtureGeneratorId.SPREADSHEET_XLSX

    def write(self, spec: GeneratedFixtureSpec, path: Path, *, task_id: str) -> None:
        """Write a deterministic one-sheet XLSX workbook.

        Args:
            spec: Spreadsheet generator spec with validated rows.
            path: Local XLSX file path to create.
            task_id: Unused case id accepted for the shared generator API.
        """

        _ = task_id
        rows = _rows_from_params(_params(spec, SpreadsheetRowsParams))
        sheet_xml = _worksheet_xml(rows)
        with zipfile.ZipFile(path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
            archive.writestr("_rels/.rels", _ROOT_RELS_XML)
            archive.writestr("xl/workbook.xml", _WORKBOOK_XML)
            archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS_XML)
            archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)


class ImagePpmGenerator(FixtureGenerator):
    """Generate a deterministic checkerboard PPM image fixture."""

    generator_id = FixtureGeneratorId.IMAGE_PPM

    def write(self, spec: GeneratedFixtureSpec, path: Path, *, task_id: str) -> None:
        """Write a deterministic ASCII PPM image.

        Args:
            spec: PPM image generator spec with dimensions and colors.
            path: Local image file path to create.
            task_id: Unused case id accepted for the shared generator API.
        """

        _ = task_id
        params = _params(spec, ImagePpmParams)
        rows: list[str] = []
        for y in range(params.height):
            values: list[str] = []
            for x in range(params.width):
                values.extend(str(value) for value in (params.first if (x + y) % 2 == 0 else params.second))
            rows.append(" ".join(values))
        path.write_text(f"P3\n{params.width} {params.height}\n255\n" + "\n".join(rows) + "\n", encoding="ascii")


@dataclass(frozen=True, slots=True)
class FixtureGeneratorRegistry:
    """Lookup table for deterministic fixture generators.

    Attributes:
        generators: Generator instances keyed by fixture generator id.
    """

    generators: dict[FixtureGeneratorId, FixtureGenerator]

    def write(self, spec: GeneratedFixtureSpec, path: Path, *, task_id: str) -> None:
        """Dispatch fixture generation to the configured generator.

        Args:
            spec: Typed generated fixture spec.
            path: Local path where the generated file should be written.
            task_id: Case id passed through to the generator.

        Raises:
            ValueError: When the generator id is not registered.
        """

        try:
            generator = self.generators[spec.generator]
        except KeyError as exc:
            raise ValueError(f"unsupported fixture generator: {spec.generator.value}") from exc
        generator.write(spec, path, task_id=task_id)


def default_fixture_generator_registry() -> FixtureGeneratorRegistry:
    """Return the built-in generator registry.

    Returns:
        Registry containing all deterministic fixture generators.
    """

    generators: list[FixtureGenerator] = [
        HtmlSimplePageGenerator(),
        TextFileGenerator(),
        SetupScriptGenerator(),
        SpreadsheetCsvGenerator(),
        SpreadsheetXlsxGenerator(),
        ImagePpmGenerator(),
    ]
    return FixtureGeneratorRegistry(generators={generator.generator_id: generator for generator in generators})


def _params[T: BaseModel](spec: GeneratedFixtureSpec, expected_type: type[T]) -> T:
    if isinstance(spec.params, expected_type):
        return spec.params
    raise TypeError(
        f"fixture generator {spec.generator.value} expected params {expected_type.__name__}, "
        f"got {type(spec.params).__name__}"
    )


def _rows_from_params(params: SpreadsheetRowsParams) -> list[list[str]]:
    return [[str(value) for value in row] for row in params.rows]


def _worksheet_xml(rows: list[list[str]]) -> str:
    cells: list[str] = []
    for row_number, row in enumerate(rows, start=1):
        cell_xml = []
        for col_number, value in enumerate(row, start=1):
            cell_ref = f"{_column_name(col_number)}{row_number}"
            if _is_number(value):
                cell_xml.append(f'<c r="{cell_ref}"><v>{escape(value)}</v></c>')
            else:
                cell_xml.append(f'<c r="{cell_ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
        cells.append(f'<row r="{row_number}">{"".join(cell_xml)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(cells)}</sheetData></worksheet>"
    )


def _column_name(index: int) -> str:
    output = io.StringIO()
    while index:
        index, remainder = divmod(index - 1, 26)
        output.write(chr(65 + remainder))
    return output.getvalue()[::-1]


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


_CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""
_ROOT_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
_WORKBOOK_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""
_WORKBOOK_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>"""
