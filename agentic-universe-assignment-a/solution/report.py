"""Writes the five report files."""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from .derive import derive, ist_iso, lead_bucket
from .normalize import text_to_ist
from .schemas import Catalog

BUCKETS = ["yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed"]


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def _bool(b: bool) -> str:
    return "true" if b else "false"


def write_reports(conn: sqlite3.Connection, catalog: Catalog, out_dir: Path | str) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    d = derive(conn, catalog)
    disp = catalog.dispositions

    def terminal(wf):
        return lambda code: disp[(wf, code)][0]

    def success(wf, code):
        return code is not None and disp[(wf, code)][1]

    batch_counts: dict[tuple, Counter] = defaultdict(Counter)
    daily: dict[tuple, dict] = {}
    lead_rows = []
    for lead_id, (wf, ref, batch, last4) in d.leads.items():
        calls = d.calls_by_lead.get(lead_id, [])
        bucket = lead_bucket(calls, terminal(wf))
        bc = batch_counts[(wf, batch)]
        bc["total"] += 1
        bc[bucket] += 1
        for c in calls:
            day = text_to_ist(c.initiated_at).date().isoformat()
            agg = daily.setdefault((wf, day), {"total": 0, "connected": 0, "success": set()})
            agg["total"] += 1
            agg["connected"] += c.connected
            if success(wf, c.disposition):
                agg["success"].add(lead_id)
        with_disp = [c for c in calls if c.disposition is not None]
        lead_rows.append((wf, ref, batch, lead_id, [
            wf, ref, batch, "+91******" + last4, bucket, len(calls),
            sum(c.connected for c in calls),
            ist_iso(calls[-1].initiated_at) if calls else "",
            with_disp[-1].disposition if with_disp else "",
            _bool(any(success(wf, c.disposition) for c in calls))]))

    _write_csv(out / "batch_summary.csv",
               ["workflow_id", "batch_id", "total_leads", *BUCKETS],
               [[wf, b, c["total"], *(c[k] for k in BUCKETS)]
                for (wf, b), c in sorted(batch_counts.items())])
    _write_csv(out / "daily_overview.csv",
               ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate",
                "success_leads"],
               [[wf, day, a["total"], a["connected"], f"{a['connected'] / a['total']:.4f}",
                 len(a["success"])] for (wf, day), a in sorted(daily.items())])
    lead_rows.sort(key=lambda r: r[:4])
    _write_csv(out / "lead_report.csv",
               ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
                "connected_calls", "last_call_at", "current_disposition", "ever_success"],
               [r[4] for r in lead_rows])
    rej = conn.execute("SELECT workflow_id, batch_id, row_number, reason FROM lead_rejections"
                       " ORDER BY workflow_id, batch_id, row_number, id").fetchall()
    _write_csv(out / "rejections.csv", ["workflow_id", "batch_id", "row_number", "reason"], rej)

    t = conn.execute("SELECT COALESCE(SUM(rows_total),0), COALESCE(SUM(rows_rejected),0),"
                     " COALESCE(SUM(dupes_merged),0), COALESCE(SUM(field_warnings),0),"
                     " COALESCE(SUM(events_total),0), COALESCE(SUM(events_malformed),0),"
                     " COALESCE(SUM(events_duplicate),0) FROM ingested_files").fetchone()
    reasons = dict(conn.execute("SELECT reason, COUNT(*) FROM lead_rejections"
                                " GROUP BY reason ORDER BY reason").fetchall())
    dq = {"lead_rows_total": t[0], "lead_rows_rejected": t[1], "rejections_by_reason": reasons,
          "duplicate_lead_rows_merged": t[2], "field_warnings": t[3], "events_total": t[4],
          "malformed_events": t[5], "duplicate_events_dropped": t[6],
          "orphan_events": d.orphan_events,
          "unknown_disposition_events": d.unknown_disposition_events}
    (out / "data_quality.json").write_text(json.dumps(dq, indent=2) + "\n", encoding="utf-8")
    return dq
