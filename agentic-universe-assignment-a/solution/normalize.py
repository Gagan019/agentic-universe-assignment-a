"""Pure value-normalisation helpers. No I/O, no DB: easy to unit test."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def normalize_header(h: str) -> str:
    """lower-case, trim, collapse every run of '_' or whitespace to one space."""
    return re.sub(r"[_\s]+", " ", h.lower().strip()).strip()


def normalize_phone(raw) -> str | None:
    """Indian mobile -> '+91XXXXXXXXXX', or None if invalid."""
    if raw is None:
        return None
    digits = re.sub(r"[^0-9]", "", str(raw))  # ASCII digits only
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


def parse_date(raw: str) -> str | None:
    """Day-first. Accepts YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY. Returns ISO or None."""
    s = raw.strip()
    y = mo = d = None
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s):
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    elif m := re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", s):
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
    elif m := re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", s):
        mo = _MONTHS.get(m[2].lower())
        d, y = int(m[1]), int(m[3])
    if mo is None:
        return None
    try:
        return date(y, mo, d).isoformat()
    except ValueError:  # impossible date, e.g. 31/02
        return None


_DECIMAL_RE = re.compile(r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d+)?|\.\d+)", re.IGNORECASE)


def parse_decimal(raw: str) -> str | None:
    """Currency amount >= 0, rounded half-up to 2 places, returned as a string."""
    m = _DECIMAL_RE.fullmatch(raw.strip())
    if not m:  # also rejects negatives and words
        return None
    value = Decimal(m[1].replace(",", ""))
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def parse_enum(raw: str, values: list[str]) -> str | None:
    v = raw.strip().lower()
    return v if v in {x.lower() for x in values} else None


def parse_timestamp(raw) -> datetime | None:
    """Z / offset / naive. Naive means IST. Returns an aware UTC datetime, or None."""
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(UTC)


TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"  # fixed width => lexicographic order == time order


def ts_to_text(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(TS_FMT)


def text_to_ist(text: str) -> datetime:
    return datetime.strptime(text, TS_FMT).replace(tzinfo=UTC).astimezone(IST)
