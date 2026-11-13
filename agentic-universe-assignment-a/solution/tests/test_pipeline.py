"""End-to-end tests on a tiny hand-written dataset plus generated data."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from solution import db as dbmod
from solution.ingest import ingest_path
from solution.pii import Pii
from solution.report import write_reports
from solution.schemas import load_catalog

ROOT = Path(__file__).resolve().parents[2]
CAT = load_catalog()


def ev(eid, typ, call, ts, phone=None, wf="motor_renewal", **payload):
    e = {"event_id": eid, "event_type": typ, "workflow_id": wf, "call_id": call,
         "occurred_at": ts, "payload": payload}
    if phone is not None:
        e["phone"] = phone
    return json.dumps(e)


def write_drop(root: Path, name: str, leads: dict[str, str], events: list[str]):
    d = root / name
    for rel, text in leads.items():
        p = d / "leads" / "motor_renewal" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (d / "events").mkdir(parents=True, exist_ok=True)
    (d / "events" / f"events_{name}.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")


def run(root, db, out, *drops):
    conn = dbmod.connect(str(db))
    pii = Pii(b"test-key")
    for dname in drops:
        ingest_path(conn, pii, CAT, root / dname, log=lambda *_: None)
    dq = write_reports(conn, CAT, out)
    conn.close()
    return dq


def read(out, name):
    return (Path(out) / name).read_text(encoding="utf-8")


HDR = "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language\n"


@pytest.fixture
def tiny(tmp_path):
    # drop_1: events arrive BEFORE their lead; lead A is uploaded in drop_2.
    A = "9876543210"
    write_drop(tmp_path, "drop_1", {
        "batch_001.csv": HDR + "Bob,9111111111,POL-B,2026-12-01,\"₹1,000\",HI\n"
                               ",,,,,\n"                      # blank row: ignored, not counted
                               "NoPhone,12345,POL-X,,,\n"     # invalid_phone (row 2)
                               ",9222222222,POL-Y,,,\n",      # missing customer_name (row 3)
    }, [
        ev("e1", "call.initiated", "c1", "2026-10-01T04:30:00Z", A),
        ev("e2", "call.connected", "c1", "2026-10-01 10:00:10", "+91 98765 43210"),
        ev("e3", "disposition.set", "c1", "2026-10-01T10:05:00+05:30", A, disposition_code="callback_requested"),
        ev("e4", "disposition.override", "c1", "2026-10-02T10:00:00Z", disposition_code="renewed"),
        ev("e1", "call.initiated", "c1", "2026-10-01T04:30:00Z", A),   # duplicate
        "{not json",
        ev("e5", "call.initiated", "c9", "2026-10-01T05:00:00Z", "9000000000"),  # orphan
        ev("e6", "disposition.set", "c9", "2026-10-01T05:10:00Z", "9000000000", disposition_code="bogus"),  # orphan, not unknown
    ])
    write_drop(tmp_path, "drop_2", {
        "batch_002.csv": "Mobile,Name,Policy No,Language\n9876543210,Alice,POL-A,\n"
                         "+919876543210,Alice A,POL-A2,ta\n",  # duplicate: merges, last write wins
    }, [ev("e7", "disposition.set", "c1", "2026-10-01T10:06:00Z", A, disposition_code="nonsense")])
    return tmp_path


def test_end_to_end_rules(tiny, tmp_path):
    dq = run(tiny, tmp_path / "a.db", tmp_path / "out", "drop_1", "drop_2")
    assert dq["lead_rows_total"] == 5 and dq["lead_rows_rejected"] == 2
    assert dq["rejections_by_reason"] == {"invalid_phone": 1, "missing_required:customer_name": 1}
    assert dq["duplicate_lead_rows_merged"] == 1
    assert dq["events_total"] == 9 and dq["malformed_events"] == 1 and dq["duplicate_events_dropped"] == 1
    assert dq["orphan_events"] == 2           # c9's two events; the early A events resolved
    assert dq["unknown_disposition_events"] == 1
    assert read(tmp_path / "out", "rejections.csv").splitlines() == [
        "workflow_id,batch_id,row_number,reason",
        "motor_renewal,batch_001,2,invalid_phone",
        "motor_renewal,batch_001,3,missing_required:customer_name"]
    lead = read(tmp_path / "out", "lead_report.csv").splitlines()
    # Alice: external_ref overwritten by the later duplicate (last write wins), batch stays batch_002
    assert "motor_renewal,POL-A2,batch_002,+91******3210,completed,1,1,2026-10-01T10:00:00+05:30,renewed,true" in lead
    daily = read(tmp_path / "out", "daily_overview.csv").splitlines()
    assert daily[1] == "motor_renewal,2026-10-01,1,1,1.0000,1"


def test_orphan_resolves_when_lead_arrives(tiny, tmp_path):
    dq1 = run(tiny, tmp_path / "b.db", tmp_path / "out1", "drop_1")
    # drop_1 only: A's 4 events (initiated, connected, set, override-via-call) + c9's 2
    assert dq1["orphan_events"] == 6
    assert "POL-A" not in read(tmp_path / "out1", "lead_report.csv")
    dq2 = run(tiny, tmp_path / "b.db", tmp_path / "out2", "drop_1", "drop_2")
    assert dq2["orphan_events"] == 2


def test_idempotent_reingest(tiny, tmp_path):
    db = tmp_path / "c.db"
    dq1 = run(tiny, db, tmp_path / "o1", "drop_1", "drop_2")
    dq2 = run(tiny, db, tmp_path / "o2", "drop_1", "drop_2", "drop_1")
    assert dq1 == dq2
    for f in ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv"]:
        assert read(tmp_path / "o1", f) == read(tmp_path / "o2", f)


def test_ledger_is_append_only(tiny, tmp_path):
    run(tiny, tmp_path / "d.db", tmp_path / "o", "drop_1")
    conn = dbmod.connect(str(tmp_path / "d.db"))
    with pytest.raises(Exception, match="append-only"):
        conn.execute("UPDATE ledger_events SET call_id='x'")
    with pytest.raises(Exception, match="append-only"):
        conn.execute("DELETE FROM ledger_events")


def test_no_plaintext_pii_at_rest(tiny, tmp_path):
    db = tmp_path / "e.db"
    run(tiny, db, tmp_path / "o", "drop_1", "drop_2")
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("e.db*"))
    for needle in [b"Alice", b"Bob", b"9876543210", b"9111111111", b"+919876543210"]:
        assert needle not in raw


@pytest.mark.parametrize("seed", [42, 7])
def test_generated_data_split_vs_all_at_once(tmp_path, seed):
    data = tmp_path / "data"
    subprocess.run([sys.executable, str(ROOT / "assignment-a-data/tools/generate_data.py"),
                    "--seed", str(seed), "--scale", "small", "--out", str(data)],
                   check=True, capture_output=True)
    dq_split = run(data, tmp_path / "s.db", tmp_path / "split", "drop_1", "drop_2")
    # all at once: point ingest at the parent directory
    conn = dbmod.connect(str(tmp_path / "all.db"))
    ingest_path(conn, Pii(b"test-key"), CAT, data, log=lambda *_: None)
    dq_all = write_reports(conn, CAT, tmp_path / "all")
    assert dq_split == dq_all
    for f in ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv"]:
        assert read(tmp_path / "split", f) == read(tmp_path / "all", f)
    # sanity: every report row count lines up
    leads = read(tmp_path / "all", "lead_report.csv").splitlines()[1:]
    batch_total = sum(int(r.split(",")[2]) for r in read(tmp_path / "all", "batch_summary.csv").splitlines()[1:])
    assert len(leads) == batch_total
    assert dq_all["lead_rows_total"] - dq_all["lead_rows_rejected"] - dq_all["duplicate_lead_rows_merged"] == len(leads)
