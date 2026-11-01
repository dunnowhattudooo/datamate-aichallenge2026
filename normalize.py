from __future__ import annotations

import math
import re
import unicodedata
from functools import lru_cache
from datetime import date, datetime, timedelta
from typing import Any


@lru_cache(maxsize=65536)
def fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return text.replace("đ", "d").replace("Đ", "D").lower()


@lru_cache(maxsize=65536)
def key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", fold(value))


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    text = str(value).strip().lower()
    if not text or text in {"-", "—", "nan", "none", "null"}:
        return None

    text = text.replace("\u00a0", "").replace(" ", "")
    text = text.replace("₫", "").replace("đ", "").replace("vnd", "")
    shorthand = re.fullmatch(r"(\d+(?:[.,]\d+)?)tr(\d{1,3})", text)
    if shorthand:
        return number(shorthand.group(1)) * 1_000_000 + int(shorthand.group(2)) * 1_000
    scale = 1.0
    suffix = re.search(r"(triệu|tr|k)$", text)
    if suffix:
        scale = 1_000_000.0 if suffix.group(1) in {"triệu", "tr"} else 1_000.0
        text = text[: suffix.start()]
        if re.fullmatch(r"\d+(?:[.,]\d+)?", text):
            return float(text.replace(",", ".")) * scale

    text = re.sub(r"[^0-9,.\-+]", "", text)
    if not text or text in {"-", "+", ".", ","}:
        return None
    if "," in text and "." in text:
        decimal_sep = "," if text.rfind(",") > text.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        text = text.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif "," in text:
        parts = text.split(",")
        if len(parts) == 2 and len(parts[-1]) in (1, 2):
            text = ".".join(parts)
        else:
            text = "".join(parts)
    elif "." in text:
        parts = text.split(".")
        if len(parts) == 2 and len(parts[-1]) == 3:
            text = "".join(parts)
        elif len(parts) > 2:
            text = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(text) * scale
    except ValueError:
        return None


def parse_date(value: Any) -> date | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        if 1 <= value <= 100_000:
            return date(1899, 12, 30) + timedelta(days=int(value))
        return None

    text = str(value).strip()
    if not text:
        return None
    text = text.replace("T", " ")
    iso = re.search(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    dmy = re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})\b", text)
    if dmy:
        day, month, year = map(int, dmy.groups())
        if year < 100:
            year += 2000 if year < 70 else 1900
        try:
            return date(year, month, day)
        except ValueError:
            return None
    vietnamese_month = re.search(r"\b(\d{1,2})\s*(?:thg|thang)\s*(\d{1,2})(?:\s*(20\d{2}))?\b", fold(text))
    if vietnamese_month:
        day, month = int(vietnamese_month.group(1)), int(vietnamese_month.group(2))
        year = int(vietnamese_month.group(3)) if vietnamese_month.group(3) else date.today().year
        try:
            return date(year, month, day)
        except ValueError:
            return None
    dmy_short = re.search(r"\b(\d{1,2})[-/.](\d{1,2})\b", text)
    if dmy_short:
        day, month = map(int, dmy_short.groups())
        try:
            return date(date.today().year, month, day)
        except ValueError:
            return None
    return None


def rounded_money(value: float) -> int:
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)
