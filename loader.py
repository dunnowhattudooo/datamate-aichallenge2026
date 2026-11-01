from __future__ import annotations

import csv
import posixpath
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .normalize import key


@dataclass
class Table:
    name: str
    source: Path
    rows: list[dict[str, Any]]

    @property
    def columns(self) -> list[str]:
        return list(self.rows[0]) if self.rows else []


HEADER_HINTS = {
    "ngay", "ngaylap", "ngayban", "ngaygiaodich", "ngaythang", "date",
    "mahoadon", "madon", "mabill", "soct", "mahanghoa", "mavach", "sku",
    "tenhang", "tenhanghoa", "mathang", "sanpham", "hanghoa", "soluong",
    "sl", "sluong", "soluongquydoi", "slquydoi", "thanhtien", "trigia",
    "cong", "amount", "tongtien", "trangthai", "tinhtrang", "khachhang",
    "nhomhang", "donvitinh", "dvt", "giaban", "giavon", "giadonvi",
    "ngaynhap", "mahang", "mahh", "masp", "mã",
}

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_REL_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference.upper())
    if not letters:
        return 0
    result = 0
    for char in letters.group(0):
        result = result * 26 + ord(char) - 64
    return result - 1


def _xlsx_date_styles(archive: zipfile.ZipFile) -> set[int]:
    try:
        root = ElementTree.fromstring(archive.read("xl/styles.xml"))
    except KeyError:
        return set()
    custom_dates: set[int] = set()
    custom = root.find(f"{{{NS_MAIN}}}numFmts")
    if custom is not None:
        for item in custom:
            fmt = item.attrib.get("formatCode", "").lower()
            if re.search(r"[ymdhis]", fmt):
                try:
                    custom_dates.add(int(item.attrib["numFmtId"]))
                except (KeyError, ValueError):
                    pass
    xfs = root.find(f"{{{NS_MAIN}}}cellXfs")
    date_styles: set[int] = set()
    if xfs is not None:
        for index, xf in enumerate(xfs):
            try:
                num_fmt = int(xf.attrib.get("numFmtId", "0"))
            except ValueError:
                continue
            if num_fmt in custom_dates or 14 <= num_fmt <= 22 or 45 <= num_fmt <= 47:
                date_styles.add(index)
    return date_styles


def _xlsx_rows(archive: zipfile.ZipFile, target: str, shared: list[str], date_styles: set[int]) -> list[list[Any]]:
    root = ElementTree.fromstring(archive.read(target))
    result: list[list[Any]] = []
    sheet_data = root.find(f".//{{{NS_MAIN}}}sheetData")
    if sheet_data is None:
        return result
    for row_node in sheet_data.findall(f"{{{NS_MAIN}}}row"):
        cells: dict[int, Any] = {}
        for cell in row_node.findall(f"{{{NS_MAIN}}}c"):
            index = _column_index(cell.attrib.get("r", "A1"))
            value_node = cell.find(f"{{{NS_MAIN}}}v")
            value: Any = value_node.text if value_node is not None else ""
            cell_type = cell.attrib.get("t")
            if cell_type == "s" and value != "":
                try:
                    value = shared[int(value)]
                except (ValueError, IndexError):
                    value = ""
            elif cell_type == "inlineStr":
                value = "".join(node.text or "" for node in cell.iter(f"{{{NS_MAIN}}}t"))
            elif cell_type == "b":
                value = value == "1"
            elif value != "":
                try:
                    numeric = float(value)
                    if cell_type == "n" or cell_type is None:
                        if numeric.is_integer():
                            numeric = int(numeric)
                        if int(cell.attrib.get("s", "0")) in date_styles and 1 <= numeric <= 100_000:
                            from datetime import date, timedelta

                            value = date(1899, 12, 30) + timedelta(days=int(numeric))
                        else:
                            value = numeric
                except (ValueError, TypeError):
                    pass
            cells[index] = value
        if cells:
            width = max(cells) + 1
            result.append([cells.get(index, "") for index in range(width)])
        else:
            result.append([])
    return result


def _choose_header(rows: list[list[Any]]) -> int:
    best_index, best_score = 0, -1
    for index, row in enumerate(rows[:50]):
        nonempty = [str(value).strip() for value in row if str(value).strip()]
        if not nonempty:
            continue
        hints = sum(1 for value in nonempty if key(value) in HEADER_HINTS)
        score = hints * 3 + min(len(nonempty), 20) / 10
        if score > best_score:
            best_index, best_score = index, score
    return best_index


def _rows_to_table(name: str, source: Path, raw_rows: list[list[Any]]) -> Table:
    header_index = _choose_header(raw_rows)
    if not raw_rows or header_index >= len(raw_rows):
        return Table(name, source, [])
    headers = [str(value).strip() for value in raw_rows[header_index]]
    used: dict[str, int] = {}
    for i, header in enumerate(headers):
        if not header:
            headers[i] = f"__column_{i + 1}"
        used[headers[i]] = used.get(headers[i], 0) + 1
        if used[headers[i]] > 1:
            headers[i] = f"{headers[i]}_{used[headers[i]]}"
    records: list[dict[str, Any]] = []
    for raw in raw_rows[header_index + 1 :]:
        if not any(str(value).strip() for value in raw):
            continue
        record = {header: raw[i] if i < len(raw) else "" for i, header in enumerate(headers)}
        records.append(record)
    return Table(name, source, records)


def _read_csv(path: Path) -> Table:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(text.splitlines(), dialect))
    return _rows_to_table(path.stem, path, rows)


def _read_xlsx(path: Path) -> list[Table]:
    tables: list[Table] = []
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = [
                "".join(node.text or "" for node in item.iter(f"{{{NS_MAIN}}}t"))
                for item in root.findall(f"{{{NS_MAIN}}}si")
            ]
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relation_map = {
            relation.attrib["Id"]: relation.attrib["Target"]
            for relation in relationships.findall(f"{{{NS_REL_PKG}}}Relationship")
        }
        date_styles = _xlsx_date_styles(archive)
        sheets = workbook.find(f"{{{NS_MAIN}}}sheets")
        if sheets is None:
            return tables
        for sheet in sheets:
            relation_id = sheet.attrib.get(f"{{{NS_REL_DOC}}}id")
            target = relation_map.get(relation_id or "")
            if not target:
                continue
            if target.startswith("/"):
                target = target.lstrip("/")
            elif not target.startswith("xl/"):
                target = posixpath.normpath(posixpath.join("xl", target))
            rows = _xlsx_rows(archive, target, shared, date_styles)
            sheet_name = sheet.attrib.get("name", "Sheet")
            table = _rows_to_table(f"{path.stem}::{sheet_name}", path, rows)
            for row in table.rows:
                row["__sheet_name__"] = sheet_name
            tables.append(table)
    return tables


def _read_sqlite(path: Path) -> list[Table]:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        tables = []
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            cursor = connection.execute(f"SELECT * FROM {quoted}")
            columns = [column[0] for column in cursor.description or ()]
            records = [dict(zip(columns, row)) for row in cursor.fetchall()]
            tables.append(Table(f"{path.stem}::{name}", path, records))
        return tables
    finally:
        connection.close()


def load_business(directory: Path) -> list[Table]:
    if not directory.is_dir():
        raise FileNotFoundError(f"Business data directory does not exist: {directory}")
    tables: list[Table] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.name.startswith("~$"):
            continue
        suffix = path.suffix.lower()
        if suffix == ".csv":
            tables.append(_read_csv(path))
        elif suffix in {".xlsx", ".xlsm"}:
            tables.extend(_read_xlsx(path))
        elif suffix in {".sqlite", ".db"}:
            tables.extend(_read_sqlite(path))
    return tables
