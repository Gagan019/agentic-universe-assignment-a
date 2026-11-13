import pytest

from solution.derive import CallFacts, effective_disposition, lead_bucket
from solution.normalize import (normalize_header, normalize_phone, parse_date, parse_decimal,
                                parse_enum, parse_timestamp, ts_to_text)


@pytest.mark.parametrize("raw,expected", [
    ("9876543210", "+919876543210"),
    ("+91 98765 43210", "+919876543210"),
    ("09876543210", "+919876543210"),
    ("91-98765-43210", "+919876543210"),
    ("(+91) 9876543210", "+919876543210"),
    ("+919876543210", "+919876543210"),
    ("9123456789", "+919123456789"),      # 10 digits starting 91: nothing is dropped
    ("5876543210", None),                 # bad prefix
    ("987654321", None),                  # 9 digits
    ("022-2345-6789", None),              # landline
    ("+1 415 555 0134", None),
    ("N/A", None), ("", None), (None, None),
])
def test_phone(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("2026-12-01", "2026-12-01"), ("01/12/2026", "2026-12-01"), ("01-12-2026", "2026-12-01"),
    ("01-Dec-2026", "2026-12-01"), ("01-DEC-2026", "2026-12-01"),
    ("31/02/2026", None), ("2026-13-01", None), ("soon", None), ("12/25/2026", None),  # day-first
])
def test_date(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("27554", "27554.00"), ("₹27,554", "27554.00"), ("Rs. 24787", "24787.00"),
    ("rs24787", "24787.00"), ("INR 25301.4", "25301.40"), ("inr25,301.456", "25301.46"),
    ("0", "0.00"), ("-5", None), ("N/A", None), ("-", None), ("twelve thousand", None),
])
def test_decimal(raw, expected):
    assert parse_decimal(raw) == expected


def test_enum_and_header():
    assert parse_enum(" HI ", ["en", "hi"]) == "hi"
    assert parse_enum("Hindi", ["en", "hi"]) is None
    assert normalize_header("  Policy_No ") == "policy no"
    assert normalize_header("MOBILE__ no") == "mobile no"


def test_timestamps_naive_is_ist():
    z = parse_timestamp("2026-10-03T04:31:00Z")
    off = parse_timestamp("2026-10-03T10:01:00+05:30")
    naive = parse_timestamp("2026-10-03 10:01:00")
    assert ts_to_text(z) == ts_to_text(off) == ts_to_text(naive)
    assert parse_timestamp("garbage") is None


def test_effective_disposition():
    sets = [("2026-10-01T10", "e1", "callback_requested"), ("2026-10-01T11", "e2", "renewed")]
    ovr = [("2026-10-02T10", "e3", "not_interested"), ("2026-10-03T10", "e4", "dnd")]
    assert effective_disposition(sets, []) == "renewed"            # latest set
    assert effective_disposition(sets, ovr) == "dnd"               # latest override wins
    assert effective_disposition([], ovr[:1]) == "not_interested"  # override with no set
    assert effective_disposition([], []) is None


def _c(i, failed=False, disp=None, connected=False):
    return CallFacts(f"c{i}", f"2026-10-0{i}", connected, failed, disp)


def test_bucket_precedence():
    term = lambda code: code in {"renewed", "dnd"}  # noqa: E731
    assert lead_bucket([], term) == "yet_to_reach_out"
    # completed beats everything else, even if the other calls failed
    assert lead_bucket([_c(1, failed=True), _c(2, disp="renewed")], term) == "completed"
    assert lead_bucket([_c(1, failed=True), _c(2, failed=True)], term) == "in_progress_telephony_failed"
    assert lead_bucket([_c(1, failed=True), _c(2)], term) == "in_progress_dialed"
    assert lead_bucket([_c(1, disp="callback_requested", connected=True)], term) == "in_progress_dialed"
