"""Derivation of calls, buckets and metrics from the ledger.

Everything is recomputed from the ledger at report time, so reports always reflect everything
ingested so far and are independent of how the data was split into drops.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .normalize import text_to_ist
from .schemas import Catalog

Tagged = tuple[str, str, str]  # (occurred_at_utc, event_id, code)


def effective_disposition(sets: Iterable[Tagged], overrides: Iterable[Tagged]) -> str | None:
    """Latest override by occurred_at, else latest set, else None. Ties broken by event_id."""
    ov = max(overrides, default=None)
    if ov:
        return ov[2]
    st = max(sets, default=None)
    return st[2] if st else None


@dataclass
class CallFacts:
    call_id: str
    initiated_at: str  # UTC text
    connected: bool
    failed: bool
    disposition: str | None


def lead_bucket(calls: list[CallFacts], is_terminal: Callable[[str], bool]) -> str:
    if not calls:
        return "yet_to_reach_out"
    if any(c.disposition is not None and is_terminal(c.disposition) for c in calls):
        return "completed"
    if all(c.failed for c in calls):
        return "in_progress_telephony_failed"
    return "in_progress_dialed"


@dataclass
class _Call:
    lead_id: int | None = None
    initiated: tuple[str, str] | None = None  # (ts, event_id) of earliest initiated
    connected: bool = False
    failed: bool = False
    sets: list[Tagged] = field(default_factory=list)
    overrides: list[Tagged] = field(default_factory=list)


@dataclass
class Derived:
    leads: dict[int, tuple]                      # id -> (workflow, external_ref, batch, last4)
    calls_by_lead: dict[int, list[CallFacts]]
    orphan_events: int
    unknown_disposition_events: int


_EVENTS_SQL = """
SELECT e.event_id, e.event_type, e.workflow_id, e.call_id, e.occurred_at_utc,
       json_extract(e.payload, '$.disposition_code'), l.id
FROM ledger_events e
LEFT JOIN leads l ON l.workflow_id = e.workflow_id AND l.phone_hash = e.phone_hash
"""


def derive(conn: sqlite3.Connection, catalog: Catalog) -> Derived:
    leads = {r[0]: r[1:] for r in conn.execute(
        "SELECT id, workflow_id, external_ref, batch_id, phone_last4 FROM leads")}
    calls: dict[tuple[str, str], _Call] = {}
    orphans = unknown = 0
    pending_overrides = []

    def apply(lead_id, eid, etype, wf, call_id, ts, code) -> None:
        nonlocal unknown
        c = calls.setdefault((wf, call_id), _Call())
        if c.lead_id is None or (etype == "call.initiated" and lead_id is not None):
            c.lead_id = lead_id if lead_id is not None else c.lead_id
        if etype == "call.initiated":
            if c.initiated is None or (ts, eid) < c.initiated:
                c.initiated = (ts, eid)
        elif etype == "call.connected":
            c.connected = True
        elif etype == "call.telephony_failed":
            c.failed = True
        elif etype in ("disposition.set", "disposition.override"):
            if not isinstance(code, str) or (wf, code) not in catalog.dispositions:
                unknown += 1
                return
            (c.overrides if etype == "disposition.override" else c.sets).append((ts, eid, code))

    for eid, etype, wf, call_id, ts, code, lead_id in conn.execute(_EVENTS_SQL):
        if lead_id is not None:
            apply(lead_id, eid, etype, wf, call_id, ts, code)
        elif etype == "disposition.override":
            pending_overrides.append((eid, etype, wf, call_id, ts, code))
        else:
            orphans += 1  # order: orphan check comes before the unknown-disposition check

    for eid, etype, wf, call_id, ts, code in pending_overrides:
        c = calls.get((wf, call_id))
        if c is not None and c.lead_id is not None:  # link through the call's other events
            apply(c.lead_id, eid, etype, wf, call_id, ts, code)
        else:
            orphans += 1

    calls_by_lead: dict[int, list[CallFacts]] = {}
    for (wf, call_id), c in calls.items():
        if c.initiated is None or c.lead_id is None:
            continue  # a call exists only if its call.initiated is present and linked
        calls_by_lead.setdefault(c.lead_id, []).append(CallFacts(
            call_id=call_id, initiated_at=c.initiated[0], connected=c.connected,
            failed=c.failed, disposition=effective_disposition(c.sets, c.overrides)))
    for lst in calls_by_lead.values():
        lst.sort(key=lambda x: (x.initiated_at, x.call_id))
    return Derived(leads, calls_by_lead, orphans, unknown)


def ist_iso(ts_text: str) -> str:
    return text_to_ist(ts_text).isoformat(timespec="seconds")
