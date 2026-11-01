from __future__ import annotations

import calendar
import csv
import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .loader import Table, load_business
from .normalize import fold, key, number, parse_date, rounded_money


ALIASES: dict[str, set[str]] = {
    "order_id": {
        "mahoadon", "madon", "mabill", "soct", "mahoadon", "madonhang",
        "sohoadon", "bill", "sodon", "mahd", "machungtu",
    },
    "date": {
        "ngay", "ngaylap", "ngayban", "ngaygiaodich", "ngaythang", "date",
        "ngaydathang", "thoigian", "thoigianlap", "gioban", "ngaydon",
        "ngayhoadon", "ngaydat",
    },
    "status": {
        "trangthai", "tinhtrang", "tiendo", "xuly", "trangthaixuly",
        "trangthaidon", "tinhtrangdon", "trangthaidonhang", "status",
    },
    "item_name": {
        "tenhang", "tenhanghoa", "hang", "hangban", "mathang", "sanpham", "hanghoa",
        "tengoi", "tensanpham", "mat hang", "ten san pham", "ten thuoc", "ten goi", "tenhangban",
    },
    "item_code": {
        "mahanghoa", "mahang", "mahh", "masp", "mavach", "mavachean",
        "mahhoa", "mãsku", "sku", "barcode", "mahangban", "mahanghoaban",
        "mahhlienquan",
    },
    "group": {
        "nhom", "nhomhang", "nhomhanghoa", "phanloai", "danhmuc",
        "nhomkhachhang",
    },
    "quantity_base": {
        "soluongquydoi", "slquydoi", "soluongle", "slquydoi", "slquydoi",
        "soluongcoquydoi", "soluongquyve", "soluongquydoi",
    },
    "quantity": {
        "soluong", "sluong", "sl", "slban", "soluongban", "soluongsanpham",
        "qty", "quantity",
    },
    "unit": {"donvitinh", "dvt", "donvi", "dvtcoso", "donvicoso", "unit"},
    "line_amount": {
        "thanhtien", "cong", "trigia", "amount", "tongtien", "tienhangdong",
        "thanhtienhang", "tienban", "giatrihangdong",
    },
    "order_total": {
        "tongtien", "tongtienhang", "thanhtien", "tongthanhtoan", "giatri",
        "giatrihang", "tongtienphieu", "tongcong",
    },
    "customer_id": {"makhachhang", "makhach", "makhachmua", "mathe", "customerid"},
    "customer_name": {
        "khachhang", "hotenkhach", "tenkhachhang", "khachlesi", "tenkhach",
        "doitac", "khach",
    },
    "payment_method": {"phuongthuctt", "hinhthucthanhtoan", "loaitt", "kenhdat"},
    "channel": {"kenhbanhang", "kenhban", "kenhdat", "nguon"},
    "employee": {"nhanvienban", "manhanvien", "nhanvien", "thungan"},
    "unit_cost": {"giavonlucban", "giavon", "giavonhang"},
    "conversion_unit": {"donviquydoi"},
    "base_unit": {"donvicoban", "donvicoso"},
    "conversion_factor": {"tilequydoi"},
}

ORDER_LEVEL_FIELDS = {
    "date", "status", "customer_id", "customer_name", "payment_method",
    "channel", "employee", "order_total",
}


def _column(row: dict[str, Any], field: str) -> Any:
    aliases = ALIASES[field]
    for column, value in row.items():
        if key(column) in aliases and value is not None and str(value).strip():
            return value
    return None


def _normal_status(value: Any) -> str:
    return key(value)


def _is_cancelled(status: Any) -> bool:
    normalized = _normal_status(status)
    return any(token in normalized for token in ("huy", "cancel"))


def _is_returned(status: Any) -> bool:
    normalized = _normal_status(status)
    return any(token in normalized for token in ("trahang", "tralai", "hoanhang", "returned"))


def _money(value: Any) -> float | None:
    return number(value)


def _value(row: dict[str, Any], field: str) -> Any:
    value = _column(row, field)
    if value is not None:
        return value
    if field == "quantity_base":
        return _column(row, "quantity")
    if field == "item_name":
        return _column(row, "item_code")
    return None


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date


class DataMateAgent:
    """Conservative, local-only QA over common sales and inventory tables."""

    def __init__(
        self,
        business_dir: str | Path,
        rulebook_path: str | Path | None = None,
        calendar_path: str | Path | None = None,
    ):
        self.business_dir = Path(business_dir)
        self.rulebook_path = Path(rulebook_path) if rulebook_path else None
        self.calendar_path = Path(calendar_path) if calendar_path else None
        self.rulebook = self._load_rulebook()
        self.lunar_calendar = self._load_calendar()
        self.exclude_returned_sales = self._rulebook_excludes_returns()
        self.week_start = self._rulebook_week_start()
        self.tet_start_day, self.tet_end_day = self._rulebook_tet_bounds()
        self.tables = load_business(self.business_dir)
        self.products: dict[str, dict[str, Any]] = {}
        self.product_names: set[str] = set()
        self._index_products()
        self.unit_conversions = self._index_unit_conversions()
        self.sales_table = self._find_sales_table()
        self.headers = self._index_order_headers()
        self.lines = self._build_lines()
        self.orders = self._build_orders()
        self.lines_by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
        for line in self.lines:
            if line["date"] is not None:
                self.lines_by_date[line["date"]].append(line)
        self.available_dates = sorted(self.lines_by_date)
        self.orders_by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
        for order in self.orders.values():
            if order["date"] is not None:
                self.orders_by_date[order["date"]].append(order)
        self.available_order_dates = sorted(self.orders_by_date)
        self.data_dates = [
            row["date"] for row in self.lines if isinstance(row.get("date"), date)
        ]
        self.data_start = min(self.data_dates) if self.data_dates else None
        self.data_end = max(self.data_dates) if self.data_dates else None
        self._ordered_product_names = sorted(
            self.product_names, key=lambda value: len(key(value)), reverse=True
        )
        self._groups = {line["group"] for line in self.lines if line["group"]}
        self._ordered_groups = sorted(self._groups, key=lambda value: len(key(value)), reverse=True)

    def _load_rulebook(self) -> str:
        if self.rulebook_path is None:
            return ""
        if not self.rulebook_path.is_file():
            raise FileNotFoundError(f"Rulebook does not exist: {self.rulebook_path}")
        return self.rulebook_path.read_text(encoding="utf-8", errors="replace")

    def _load_calendar(self) -> dict[tuple[int, int, int], date]:
        if self.calendar_path is None:
            return {}
        if not self.calendar_path.is_file():
            raise FileNotFoundError(f"Solar-lunar calendar does not exist: {self.calendar_path}")
        result: dict[tuple[int, int, int], date] = {}
        with self.calendar_path.open(encoding="utf-8-sig", newline="") as file:
            for row in csv.DictReader(file):
                solar = parse_date(row.get("date"))
                try:
                    lunar = (
                        int(row["lunar_year"]),
                        int(row["lunar_month"]),
                        int(row["lunar_day"]),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if solar is not None:
                    result[lunar] = solar
        return result

    def _rulebook_excludes_returns(self) -> bool:
        if not self.rulebook:
            return True
        lines = [fold(line) for line in self.rulebook.splitlines()]
        relevant = next((line for line in lines if "don tinh vao doanh thu" in line), "")
        if not relevant:
            return True
        return "tra hang" in relevant and any(
            marker in relevant for marker in ("bo ca don", "bo don", "khong tinh", "loai")
        )

    def _rulebook_week_start(self) -> int:
        normalized = fold(self.rulebook)
        match = re.search(r"tuan.{0,80}?bat dau\s+thu\s+(hai|ba|tu|nam|sau|bay)", normalized)
        if not match:
            return 0
        weekdays = {"hai": 0, "ba": 1, "tu": 2, "nam": 3, "sau": 4, "bay": 5}
        return weekdays[match.group(1)]

    def _rulebook_tet_bounds(self) -> tuple[int, int]:
        normalized = fold(self.rulebook)
        start = re.search(r"(\d{1,2})\s+thang chap", normalized)
        end = re.search(r"(?:mung\s*)?(\d{1,2})\s+thang gieng", normalized)
        return (
            int(start.group(1)) if start else 23,
            int(end.group(1)) if end else 10,
        )

    def _index_products(self) -> None:
        for table in self.tables:
            for row in table.rows:
                name = _column(row, "item_name")
                code = _column(row, "item_code")
                if name is None:
                    continue
                normalized = key(name)
                if len(normalized) < 2:
                    continue
                self.product_names.add(str(name).strip())
                identifiers = {
                    key(value)
                    for column, value in row.items()
                    if key(column) in ALIASES["item_code"] and value is not None and str(value).strip()
                }
                identifiers.add(normalized)
                for identifier in identifiers:
                    if identifier:
                        self.products.setdefault(identifier, row)

    def _index_unit_conversions(self) -> dict[str, tuple[str, str, float]]:
        conversions: dict[str, tuple[str, str, float]] = {}
        for table in self.tables:
            for row in table.rows:
                name = _column(row, "item_name")
                large_unit = _column(row, "conversion_unit")
                base_unit = _column(row, "base_unit")
                factor = number(_column(row, "conversion_factor"))
                if name is not None and large_unit is not None and base_unit is not None and factor:
                    conversions[key(name)] = (key(large_unit), str(base_unit).strip(), factor)
        return conversions

    def _find_sales_table(self) -> Table | None:
        candidates: list[tuple[int, Table]] = []
        for table in self.tables:
            if not table.rows:
                continue
            columns = {key(column) for column in table.columns}
            has_item = bool(columns & ALIASES["item_name"] or columns & ALIASES["item_code"])
            has_qty = bool(columns & ALIASES["quantity_base"] or columns & ALIASES["quantity"])
            has_amount = bool(columns & ALIASES["line_amount"])
            has_order_or_date = bool(columns & ALIASES["order_id"] or columns & ALIASES["date"])
            score = 4 * has_item + 3 * has_qty + 3 * has_amount + 2 * has_order_or_date
            if has_item and (has_qty or has_amount) and has_order_or_date:
                score += 4
            if score >= 5:
                candidates.append((score, table))
        if not candidates:
            return None
        best_score = max(score for score, _ in candidates)
        best_tables = [table for score, table in candidates if score == best_score]
        if len(best_tables) == 1:
            return best_tables[0]
        merged: list[dict[str, Any]] = []
        seen_rows: set[tuple[Any, ...]] = set()
        for table in best_tables:
            for row in table.rows:
                signature = tuple(sorted((str(column), str(value)) for column, value in row.items()))
                if signature not in seen_rows:
                    seen_rows.add(signature)
                    merged.append(row)
        return Table("merged sales tables", best_tables[0].source, merged)

    def _index_order_headers(self) -> dict[str, dict[str, Any]]:
        headers: dict[str, dict[str, Any]] = {}
        for table in self.tables:
            for row in table.rows:
                order_id = _column(row, "order_id")
                if order_id is None:
                    continue
                has_item = _column(row, "item_name") is not None or _column(row, "item_code") is not None
                if has_item:
                    continue
                identifier = str(order_id).strip()
                existing = headers.setdefault(identifier, {})
                for field in ORDER_LEVEL_FIELDS:
                    value = _column(row, field)
                    if value is not None and field not in existing:
                        existing[field] = value
        return headers

    def _build_lines(self) -> list[dict[str, Any]]:
        if self.sales_table is None:
            return []
        rows = self.sales_table.rows
        result: list[dict[str, Any]] = []
        last_order: Any = None
        order_values: dict[Any, dict[str, Any]] = defaultdict(dict)
        seen: set[tuple[Any, ...]] = set()

        for raw in rows:
            row = dict(raw)
            order_id = _column(row, "order_id")
            if order_id is None and last_order is not None:
                order_id = last_order
                order_column = next(
                    (column for column in row if key(column) in ALIASES["order_id"]),
                    None,
                )
                if order_column:
                    row[order_column] = order_id
            elif order_id is not None:
                last_order = order_id

            order_key = str(order_id).strip() if order_id is not None else None
            if order_key is not None:
                for field in ORDER_LEVEL_FIELDS:
                    value = _column(row, field)
                    if value is not None:
                        order_values[order_key][field] = value
                    elif field in order_values[order_key]:
                        row_column = next(
                            (column for column in row if key(column) in ALIASES[field]),
                            None,
                        )
                        if row_column:
                            row[row_column] = order_values[order_key][field]

            item = _column(row, "item_name")
            item_code = _column(row, "item_code")
            product = self.products.get(key(item_code)) if item_code is not None else None
            if item is None and product is not None:
                item = _column(product, "item_name")
            if item is None and item_code is None:
                continue

            header = self.headers.get(order_key or "", {})
            raw_date = _column(row, "date") or header.get("date")
            row_date = _parse_data_date(raw_date, row.get("__sheet_name__"))
            status = _column(row, "status") or header.get("status")
            base_quantity = _column(row, "quantity_base")
            raw_quantity = base_quantity if base_quantity is not None else _column(row, "quantity")
            quantity = number(raw_quantity)
            amount = _money(_column(row, "line_amount"))
            unit = _column(row, "unit") or (_column(product, "unit") if product else None)
            conversion = self.unit_conversions.get(key(item))
            if (
                base_quantity is None
                and quantity is not None
                and unit is not None
                and conversion is not None
                and key(unit) == conversion[0]
            ):
                quantity *= conversion[2]
                unit = conversion[1]
            category = _column(row, "group") or (_column(product, "group") if product else None)
            identity = tuple(
                sorted(
                    (str(k), str(v))
                    for k, v in raw.items()
                    if k != "__sheet_name__"
                )
            )
            dedupe = (order_key, identity)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            result.append(
                {
                    "order_id": order_key,
                    "date": row_date,
                    "status": status,
                    "item": str(item).strip() if item is not None else str(item_code),
                    "item_code": str(item_code).strip() if item_code is not None else None,
                    "group": str(category).strip() if category is not None else None,
                    "unit": str(unit).strip() if unit is not None else None,
                    "quantity": quantity,
                    "amount": amount,
                    "raw": row,
                }
            )
        return result

    def _build_orders(self) -> dict[str, dict[str, Any]]:
        orders: dict[str, dict[str, Any]] = {}
        for line in self.lines:
            order_id = line["order_id"]
            if order_id is None:
                continue
            order = orders.setdefault(
                order_id,
                {
                    "date": line["date"],
                    "status": line["status"],
                    "revenue": 0.0,
                    "has_revenue": False,
                },
            )
            if order["date"] is None:
                order["date"] = line["date"]
            if order["status"] is None:
                order["status"] = line["status"]
            if line["amount"] is not None:
                order["revenue"] += line["amount"]
                order["has_revenue"] = True
        for order_id, header in self.headers.items():
            order = orders.setdefault(
                order_id,
                {
                    "date": None,
                    "status": None,
                    "revenue": 0.0,
                    "has_revenue": False,
                },
            )
            if order["date"] is None:
                order["date"] = parse_date(header.get("date"))
            if order["status"] is None:
                order["status"] = header.get("status")
        return orders

    def answer(self, question: str, as_of_date: str) -> dict[str, Any]:
        as_of = parse_date(as_of_date)
        if as_of is None:
            return {"type": "abstain"}
        query = fold(question)
        query_key = key(question)
        if _asks_comparison(query_key):
            return {"type": "abstain"}
        period = _parse_period(
            query,
            as_of,
            self.lunar_calendar,
            week_start=self.week_start,
            tet_start_day=self.tet_start_day,
            tet_end_day=self.tet_end_day,
        )
        if period is None:
            if _has_unsupported_time_expression(query_key):
                return {"type": "abstain"}
            if self.data_dates:
                period = DateRange(min(self.data_dates), max(self.data_dates))
            else:
                return {"type": "abstain"}
        if not self._period_has_data(period):
            return {"type": "unanswerable", "reason": "Kỳ hỏi nằm ngoài khoảng ngày có dữ liệu."}

        if _asks_cancel_rate(query_key):
            return self._cancel_rate(period)

        selected = self._filtered_lines(period, query, query_key)
        asks_top = _asks_top(query_key)
        asks_orders = _asks_order_count(query_key)
        asks_average = _asks_average(query_key)
        asks_percent = "%" in question or any(word in query_key for word in ("tile", "phantram", "baonhieuphantram"))
        asks_revenue = _asks_revenue(query_key)
        asks_quantity = _asks_quantity(query_key)
        specific_item = self._find_mentioned_item(query_key)
        mentioned_group = self._find_mentioned_group(query_key)
        if "nhom" in query_key and mentioned_group is None:
            return {"type": "unanswerable", "reason": "Dữ liệu không có thông tin nhóm hàng phù hợp."}

        if asks_top:
            ranked = self._rank_items(selected, query_key)
            if not ranked:
                return {"type": "unanswerable", "reason": "Dữ liệu không có mặt hàng phù hợp trong kỳ hỏi."}
            limit = _top_limit(query)
            names = [name for name, _ in ranked[:limit]]
            if limit == 1 and not re.search(r"\b(?:top|liet ke|ke ten|nhung mon|cac mon)\b", query):
                return {"type": "string", "answer": names[0]}
            return {"type": "list", "answer": names}

        if asks_orders:
            order_ids = self._valid_order_ids(period, selected)
            if not order_ids:
                return {"type": "unanswerable", "reason": "Không tìm thấy bảng đơn hàng có ngày và mã đơn."}
            return {"type": "number", "answer": len(order_ids)}

        if asks_average:
            order_ids = self._valid_order_ids(period, selected)
            revenues = [
                self.orders[identifier]["revenue"]
                for identifier in order_ids
                if self.orders.get(identifier, {}).get("has_revenue")
            ]
            if not revenues:
                return {"type": "abstain"}
            return {"type": "number", "answer": rounded_money(sum(revenues) / len(revenues))}

        if specific_item and asks_quantity:
            matched = [line for line in selected if key(line["item"]) == key(specific_item)]
            if not matched:
                return {"type": "number", "answer": 0}
            requested_unit = _requested_unit(query_key)
            if requested_unit:
                matched = [line for line in matched if line["unit"] and key(line["unit"]) == requested_unit]
            quantities = [line["quantity"] for line in matched if line["quantity"] is not None]
            if not quantities:
                return {"type": "abstain"}
            return {"type": "number", "answer": _compact_number(sum(quantities))}

        if asks_revenue:
            if specific_item:
                selected = [line for line in selected if key(line["item"]) == key(specific_item)]
            else:
                if mentioned_group:
                    selected = [line for line in selected if line["group"] and key(line["group"]) == key(mentioned_group)]
            if not selected or not any(line["amount"] is not None for line in selected):
                if specific_item or not self.lines:
                    return {"type": "abstain"}
                return {"type": "number", "answer": 0}
            return {"type": "number", "answer": rounded_money(sum(line["amount"] or 0 for line in selected))}

        if asks_percent:
            return self._percent_answer(selected, period, query_key)

        if asks_quantity:
            requested_unit = _requested_unit(query_key)
            if requested_unit:
                selected = [line for line in selected if line["unit"] and key(line["unit"]) == requested_unit]
            quantities = [line["quantity"] for line in selected if line["quantity"] is not None]
            if not quantities:
                return {"type": "abstain"}
            return {"type": "number", "answer": _compact_number(sum(quantities))}

        return {"type": "abstain"}

    def _period_has_data(self, period: DateRange) -> bool:
        if self.data_start is None or self.data_end is None:
            return False
        return not (period.end < self.data_start or period.start > self.data_end)

    def _filtered_lines(self, period: DateRange, query: str, query_key: str) -> list[dict[str, Any]]:
        start = bisect_left(self.available_dates, period.start)
        end = bisect_right(self.available_dates, period.end)
        selected = [
            line
            for day in self.available_dates[start:end]
            for line in self.lines_by_date[day]
            if not _is_cancelled(line["status"])
            and (not self.exclude_returned_sales or not _is_returned(line["status"]))
        ]
        if "ca sang" in query or "casang" in query_key:
            selected = [line for line in selected if _in_shift(line, morning=True)]
        elif "ca chieu" in query or "cachieu" in query_key:
            selected = [line for line in selected if _in_shift(line, morning=False)]
        group = self._find_mentioned_group(query_key)
        if group and not _asks_revenue(query_key):
            selected = [line for line in selected if line["group"] and key(line["group"]) == key(group)]
        return selected

    def _find_mentioned_item(self, query_key: str) -> str | None:
        for name in self._ordered_product_names:
            normalized = key(name)
            if len(normalized) >= 3 and normalized in query_key:
                return name
        return None

    def _find_mentioned_group(self, query_key: str) -> str | None:
        return next(
            (group for group in self._ordered_groups
             if len(key(group)) >= 2 and key(group) in query_key),
            None,
        )

    def _rank_items(self, selected: list[dict[str, Any]], query_key: str) -> list[tuple[str, float]]:
        counts: dict[str, float] = defaultdict(float)
        codes: dict[str, str] = {}
        for line in selected:
            if line["item"] and key(line["item"]) != "phiphucvu":
                counts[line["item"]] += line["quantity"] or 0
                codes[line["item"]] = line["item_code"] or line["item"]
        if not counts:
            return []
        return sorted(counts.items(), key=lambda item: (-item[1], key(codes[item[0]]), key(item[0])))

    def _valid_order_ids(self, period: DateRange, selected: list[dict[str, Any]]) -> set[str]:
        identifiers = {
            line["order_id"] for line in selected if line["order_id"] is not None
        }
        return {
            identifier for identifier in identifiers
            if identifier in self.orders
            and self.orders[identifier]["date"] is not None
            and period.start <= self.orders[identifier]["date"] <= period.end
            and not _is_cancelled(self.orders[identifier]["status"])
            and (not self.exclude_returned_sales or not _is_returned(self.orders[identifier]["status"]))
        }

    def _cancel_rate(self, period: DateRange) -> dict[str, Any]:
        start = bisect_left(self.available_order_dates, period.start)
        end = bisect_right(self.available_order_dates, period.end)
        orders = [
            order
            for day in self.available_order_dates[start:end]
            for order in self.orders_by_date.get(day, ())
        ]
        if not orders:
            return {"type": "unanswerable", "reason": "Không có đơn hàng trong kỳ hỏi."}
        cancelled = sum(_is_cancelled(order["status"]) for order in orders)
        return {"type": "percent", "answer": round(cancelled * 100 / len(orders) + 1e-10, 2)}

    def _percent_answer(
        self, selected: list[dict[str, Any]], period: DateRange, query_key: str
    ) -> dict[str, Any]:
        if "doanhthu" in query_key or "dthu" in query_key or "tienban" in query_key:
            group = self._find_mentioned_group(query_key)
            if group:
                group_amount = sum(line["amount"] or 0 for line in selected if line["group"] and key(line["group"]) == key(group))
                all_amount = sum(line["amount"] or 0 for line in selected)
                if all_amount == 0:
                    return {"type": "unanswerable", "reason": "Doanh thu kỳ so sánh bằng 0."}
                return {"type": "percent", "answer": round(group_amount * 100 / all_amount + 1e-10, 2)}
        return {"type": "abstain"}


def _compact_number(value: float) -> int | float:
    rounded = round(value, 2)
    return int(rounded) if rounded.is_integer() else rounded


def _parse_data_date(value: Any, sheet_name: Any = None) -> date | None:
    parsed = parse_date(value)
    year_match = re.search(r"\b(20\d{2})\b", str(sheet_name or ""))
    if not year_match or not isinstance(value, str):
        return parsed
    if re.search(r"\b20\d{2}\b", value):
        return parsed
    short_date = re.search(r"\b(\d{1,2})[/.-](\d{1,2})\b", value)
    if not short_date:
        return parsed
    day, month = map(int, short_date.groups())
    try:
        return date(int(year_match.group(1)), month, day)
    except ValueError:
        return parsed


def _asks_cancel_rate(query: str) -> bool:
    return ("huy" in query) and any(
        word in query for word in ("tile", "phantram", "baonhieu", "may")
    )


def _asks_comparison(query: str) -> bool:
    return any(
        marker in query
        for marker in (
            "sosanh", "sovoi", "honhaykem", "caohaythap", "tangtruong",
            "thaydoi", "cungky", "kytruoc", "chenhlech", "tanghaygiam",
            "giamhaytang",
        )
    )


def _has_unsupported_time_expression(query: str) -> bool:
    return any(
        marker in query
        for marker in (
            "tet", "quy", "namngoai", "ngaygan", "ngaytruoc", "ngaysau",
            "tungay", "denngay", "tuthang", "denthang", "thangtruoc",
            "6thang", "3thang", "12thang",
        )
    )


def _asks_top(query: str) -> bool:
    if any(word in query for word in ("banchay", "hotnhat", "loinhat")):
        return True
    has_product = any(word in query for word in ("mathang", "sanpham", "hanghoa", "mon", "masp", "sp"))
    return has_product and "nhieu" in query and "nhat" in query and "khach" not in query


def _asks_order_count(query: str) -> bool:
    has_order = any(word in query for word in ("donhang", "hoadon", "madon", "don"))
    has_count = any(word in query for word in ("baonhieu", "maydon", "sodon", "dem", "soluong"))
    return has_order and has_count and not _asks_average(query)


def _asks_average(query: str) -> bool:
    return any(word in query for word in ("trungbinh", "binhquan", "average", "moidon"))


def _asks_revenue(query: str) -> bool:
    return any(word in query for word in ("doanhthu", "dthu", "tienban", "tienhang", "tongtien", "baonhieutien", "baonhieudong"))


def _asks_quantity(query: str) -> bool:
    return any(word in query for word in ("baonhieu", "bnhiu", "may", "tongcong"))


def _requested_unit(query: str) -> str | None:
    units = sorted({
        "chai", "ly", "lon", "goi", "cai", "kg", "gam", "gram", "hop",
        "thung", "vien", "que", "cay", "bo", "loc", "vi", "lit",
    }, key=len, reverse=True)
    return next((unit for unit in units if unit in query), None)


def _top_limit(query: str) -> int:
    match = re.search(r"\b(?:top\s*)?(\d{1,2})\s*(?:mon|mat hang|san pham|hang|sp)\b", query)
    return min(int(match.group(1)), 20) if match else 1


def _in_shift(line: dict[str, Any], morning: bool) -> bool:
    raw = line["raw"]
    for column, value in raw.items():
        column_name = key(column)
        if column_name in {"gioban", "thoigianlap", "thoigian", "time"}:
            date_time = str(value)
            hour_match = re.search(r"(?:T)?(\d{1,2}):(\d{2})", date_time)
            if hour_match:
                hour = int(hour_match.group(1))
                return 6 <= hour < 14 if morning else 14 <= hour < 22
            serial = number(value)
            if serial is not None and 1 <= serial <= 100_000:
                fraction = serial % 1
                hour = int(fraction * 24)
                return 6 <= hour < 14 if morning else 14 <= hour < 22
    return False


def _parse_period(
    question: str,
    as_of: date,
    lunar_calendar: dict[tuple[int, int, int], date] | None = None,
    week_start: int = 0,
    tet_start_day: int = 23,
    tet_end_day: int = 10,
) -> DateRange | None:
    q = fold(question)
    query = key(question)
    if any(token in query for token in ("homnay", "buannay", "buanay", "homni", "hnay", "ngayhomnay")):
        return DateRange(as_of, as_of)
    if "homqua" in query:
        yesterday = as_of - timedelta(days=1)
        return DateRange(yesterday, yesterday)
    if "tuantruoc" in query or "tuanroi" in query:
        this_week_start = as_of - timedelta(days=(as_of.weekday() - week_start) % 7)
        start = this_week_start - timedelta(days=7)
        return DateRange(start, start + timedelta(days=6))
    if "tuannay" in query:
        start = as_of - timedelta(days=(as_of.weekday() - week_start) % 7)
        return DateRange(start, as_of)
    if "thangnay" in query:
        return DateRange(as_of.replace(day=1), as_of)
    if "daunam" in query or "tudaunam" in query or "tudaunamtoinay" in query:
        return DateRange(date(as_of.year, 1, 1), as_of)
    if "tet" in query:
        year_match = re.search(
            r"\btet(?:\s+am\s+lich)?\s*(?:nam\s*)?(20\d{2})\b", q
        )
        lunar_year = int(year_match.group(1)) if year_match else as_of.year
        calendar_dates = lunar_calendar or {}
        start = calendar_dates.get((lunar_year - 1, 12, tet_start_day))
        end = calendar_dates.get((lunar_year, 1, tet_end_day))
        if start is not None and end is not None:
            return DateRange(start, end)

    month_pattern = re.search(
        r"(?:thang|th|t)\s*(0?[1-9]|1[0-2])\s*(?:/|-|\s*(?:nam)\s*)\s*(\d{2,4})",
        q,
    )
    if month_pattern:
        month, year = int(month_pattern.group(1)), int(month_pattern.group(2))
        if year < 100:
            year += 2000 if year < 70 else 1900
        return _month_range(year, month)
    month_year = re.search(r"\b(0?[1-9]|1[0-2])\s*/\s*(\d{2,4})\b", q)
    if month_year:
        month, year = int(month_year.group(1)), int(month_year.group(2))
        if year < 100:
            year += 2000 if year < 70 else 1900
        return _month_range(year, month)
    month_only = re.search(r"\b(?:thang|th|t)\s*(0?[1-9]|1[0-2])\b", q)
    if month_only:
        month = int(month_only.group(1))
        year = as_of.year if month <= as_of.month else as_of.year - 1
        return _month_range(year, month)

    explicit_date = re.search(r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b", q)
    if explicit_date:
        parsed = parse_date(explicit_date.group(0))
        if parsed:
            return DateRange(parsed, parsed)
    year = re.search(r"\b(20\d{2})\b", q)
    if year and any(word in query for word in ("nam", "trongnam", "ca nam")):
        return DateRange(date(int(year.group(1)), 1, 1), date(int(year.group(1)), 12, 31))
    return None


def _month_range(year: int, month: int) -> DateRange:
    last_day = calendar.monthrange(year, month)[1]
    return DateRange(date(year, month, 1), date(year, month, last_day))
