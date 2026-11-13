"""Ingestion of lead CSVs and event JSONL files.

Each file is ingested in ONE transaction together with its `ingested_files` row, so a crash
leaves either everything or nothing from that file. A file whose (file_key, sha256) is already
recorded is skipped, which is what makes re-running `ingest` a no-op.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .normalize import (normalize_header, normalize_phone, parse_date, parse_decimal,
                        parse_enum, parse_timestamp, ts_to_text)
from .pii import Pii
from .schemas import Catalog, Schema


# ----------------------------------------------------------------------------- lead rows
@dataclass
class RowResult:
    reject_reason: str | None = None
    values: dict | None = None  # field name -> normalised value or None
    warnings: int = 0


def _parse_value(f, raw: str):
    """Returns the normalised value, or None if the (non-empty) value is invalid."""
    if f.type == "string":
        return raw
    if f.type == "phone":
        return normalize_phone(raw)
    if f.type == "date":
        return parse_date(raw)
    if f.type == "decimal":
        return parse_decimal(raw)
    if f.type == "enum":
        return parse_enum(raw, list(f.values))
    raise ValueError(f"unsupported field type {f.type!r}")


def map_headers(schema: Schema, header: list[str]) -> dict[str, int]:
    """field name -> column index. First matching column wins; unmatched columns are ignored."""
    lookup: dict[str, str] = {}
    for f in schema.fields:
        for label in (f.name, *f.aliases):
            lookup.setdefault(normalize_header(label), f.name)
    mapping: dict[str, int] = {}
    for idx, h in enumerate(header):
        name = lookup.get(normalize_header(h))
        if name and name not in mapping:
            mapping[name] = idx
    return mapping


def process_row(schema: Schema, mapping: dict[str, int], row: list[str]) -> RowResult:
    values: dict = {}
    warnings = 0
    for f in schema.fields:  # schema order: first failure is the single reason
        idx = mapping.get(f.name)
        raw = row[idx].strip() if idx is not None and idx < len(row) else ""
        if raw == "":
            if f.required:
                return RowResult(reject_reason=f"missing_required:{f.name}")
            values[f.name] = None
            continue
        parsed = _parse_value(f, raw)
        if parsed is None:
            if f.type == "phone":
                return RowResult(reject_reason="invalid_phone")
            if f.required:  # not covered by the spec; see SUBMISSION.md
                return RowResult(reject_reason=f"invalid_value:{f.name}")
            warnings += 1
        values[f.name] = parsed
    return RowResult(values=values, warnings=warnings)


# ----------------------------------------------------------------------------- stores
def _upsert_lead(conn, pii: Pii, schema: Schema, batch_id: str, values: dict) -> bool:
    """Create the lead, or merge into the existing one. Returns True if it was a merge."""
    phone = values["phone"]
    phash = pii.phone_hash(phone)
    row = conn.execute(
        "SELECT id, external_ref, attrs, pii_enc FROM leads WHERE workflow_id=? AND phone_hash=?",
        (schema.workflow_id, phash)).fetchone()
    merged: dict = {}
    is_merge = row is not None
    if is_merge:
        merged.update(json.loads(row[2]))
        merged.update(pii.decrypt(row[3]))
        merged[schema.reference_field.name] = row[1]
    for f in schema.fields:  # last write wins, but only where the new value is non-null
        if values.get(f.name) is not None:
            merged[f.name] = values[f.name]
        else:
            merged.setdefault(f.name, None)
    for f in schema.fields:  # defaults apply after merging, to fields still null
        if merged.get(f.name) is None and f.default is not None:
            merged[f.name] = f.default
    ref_name = schema.reference_field.name
    pii_part = {f.name: merged[f.name] for f in schema.fields if f.pii}
    attrs = {f.name: merged[f.name] for f in schema.fields if not f.pii and f.name != ref_name}
    blob = pii.encrypt(pii_part)
    if is_merge:
        conn.execute("UPDATE leads SET external_ref=?, attrs=?, pii_enc=? WHERE id=?",
                     (merged[ref_name], json.dumps(attrs, sort_keys=True), blob, row[0]))
    else:
        conn.execute(
            "INSERT INTO leads (workflow_id, phone_hash, phone_last4, batch_id, external_ref,"
            " attrs, pii_enc) VALUES (?,?,?,?,?,?,?)",
            (schema.workflow_id, phash, phone[-4:], batch_id, merged[ref_name],
             json.dumps(attrs, sort_keys=True), blob))
    return is_merge


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ingest_lead_file(conn: sqlite3.Connection, pii: Pii, schema: Schema, path: Path,
                     file_key: str, sha: str) -> dict:
    batch_id = path.stem
    stats = dict(rows_total=0, rows_rejected=0, dupes_merged=0, field_warnings=0)
    conn.execute("BEGIN")
    try:
        # ingested_files row first (rejections reference it); counters updated at the end
        conn.execute("INSERT INTO ingested_files (file_key, sha256, kind, workflow_id, batch_id,"
                     " ingested_at) VALUES (?,?, 'leads', ?, ?, ?)",
                     (file_key, sha, schema.workflow_id, batch_id, _now()))
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            header = None
            row_number = 0
            mapping: dict[str, int] = {}
            for row in reader:
                if not any(c.strip() for c in row):
                    continue  # blank row: skipped, not counted
                if header is None:
                    header = row
                    mapping = map_headers(schema, header)
                    continue
                row_number += 1
                stats["rows_total"] += 1
                res = process_row(schema, mapping, row)
                if res.reject_reason:
                    stats["rows_rejected"] += 1
                    conn.execute("INSERT INTO lead_rejections (file_key, workflow_id, batch_id,"
                                 " row_number, reason) VALUES (?,?,?,?,?)",
                                 (file_key, schema.workflow_id, batch_id, row_number,
                                  res.reject_reason))
                    continue
                stats["field_warnings"] += res.warnings
                if _upsert_lead(conn, pii, schema, batch_id, res.values):
                    stats["dupes_merged"] += 1
        conn.execute("UPDATE ingested_files SET rows_total=:rows_total, rows_rejected=:rows_rejected,"
                     " dupes_merged=:dupes_merged, field_warnings=:field_warnings"
                     " WHERE file_key=:k", {**stats, "k": file_key})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return stats


# ----------------------------------------------------------------------------- events
def parse_event_line(line: bytes, pii: Pii) -> tuple | None:
    """Returns a ledger row tuple, or None if the line is malformed.

    Malformed = not valid JSON, not an object, no usable event_id, or (our addition) no
    event_type / unparseable occurred_at, because such an event cannot be placed in time.
    """
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    eid = obj.get("event_id")
    if isinstance(eid, bool) or not isinstance(eid, (str, int)) or str(eid).strip() == "":
        return None
    etype = obj.get("event_type")
    when = parse_timestamp(obj.get("occurred_at"))
    if not isinstance(etype, str) or when is None:
        return None
    phone = normalize_phone(obj.get("phone"))
    payload = obj.get("payload")
    wf, call = obj.get("workflow_id"), obj.get("call_id")
    return (str(eid).strip(), etype, str(wf) if wf is not None else None,
            str(call) if call is not None else None, ts_to_text(when),
            pii.phone_hash(phone) if phone else None,
            json.dumps(payload if isinstance(payload, dict) else {}, sort_keys=True))


def ingest_event_file(conn: sqlite3.Connection, pii: Pii, path: Path, file_key: str,
                      sha: str) -> dict:
    stats = dict(events_total=0, events_malformed=0, events_duplicate=0)
    conn.execute("BEGIN")
    try:
        conn.execute("INSERT INTO ingested_files (file_key, sha256, kind, ingested_at)"
                     " VALUES (?,?, 'events', ?)", (file_key, sha, _now()))
        with open(path, "rb") as fh:
            for line in fh:
                if not line.strip():
                    continue
                stats["events_total"] += 1
                ev = parse_event_line(line, pii)
                if ev is None:
                    stats["events_malformed"] += 1
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO ledger_events (event_id, event_type, workflow_id,"
                    " call_id, occurred_at_utc, phone_hash, payload, file_key)"
                    " VALUES (?,?,?,?,?,?,?,?)", (*ev, file_key))
                if cur.rowcount == 0:
                    stats["events_duplicate"] += 1
        conn.execute("UPDATE ingested_files SET events_total=:events_total,"
                     " events_malformed=:events_malformed, events_duplicate=:events_duplicate"
                     " WHERE file_key=:k", {**stats, "k": file_key})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return stats


# ----------------------------------------------------------------------------- drops
def _natural_key(p: Path):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.name)]


def discover_drops(path: Path) -> list[Path]:
    """`path` is either one drop (has leads/ or events/) or a parent holding several drops."""
    if (path / "leads").is_dir() or (path / "events").is_dir():
        return [path]
    return sorted((d for d in path.iterdir()
                   if d.is_dir() and ((d / "leads").is_dir() or (d / "events").is_dir())),
                  key=_natural_key)


@dataclass
class IngestReport:
    ingested: int = 0
    skipped: int = 0
    errors: int = 0


def ingest_path(conn: sqlite3.Connection, pii: Pii, catalog: Catalog, path: Path,
                log=print) -> IngestReport:
    rep = IngestReport()
    drops = discover_drops(Path(path))
    if not drops:
        raise FileNotFoundError(f"no drops (leads/ or events/ folders) found under {path}")
    for drop in drops:
        work: list[tuple[str, Path, Schema | None]] = []
        leads_dir = drop / "leads"
        if leads_dir.is_dir():
            for wf_dir in sorted(p for p in leads_dir.iterdir() if p.is_dir()):
                schema = catalog.schemas.get(wf_dir.name)
                for f in sorted(wf_dir.glob("*.csv"), key=lambda p: p.name):
                    work.append(("leads", f, schema))
        events_dir = drop / "events"
        if events_dir.is_dir():
            for f in sorted(events_dir.glob("*.jsonl"), key=lambda p: p.name):
                work.append(("events", f, None))
        for kind, f, schema in work:
            key = f"{drop.name}/{f.relative_to(drop).as_posix()}"
            sha = sha256_file(f)
            prev = conn.execute("SELECT sha256 FROM ingested_files WHERE file_key=?",
                                (key,)).fetchone()
            if prev:
                if prev[0] == sha:
                    log(f"skip   {key} (already ingested)")
                    rep.skipped += 1
                else:
                    log(f"ERROR  {key} was ingested before with different content; "
                        f"refusing to re-ingest (the ledger is append-only)")
                    rep.errors += 1
                continue
            if kind == "leads":
                if schema is None:
                    log(f"ERROR  {key}: no schema for workflow {f.parent.name!r}; skipped")
                    rep.errors += 1
                    continue
                s = ingest_lead_file(conn, pii, schema, f, key, sha)
            else:
                s = ingest_event_file(conn, pii, f, key, sha)
            log(f"ingest {key} {s}")
            rep.ingested += 1
    return rep
