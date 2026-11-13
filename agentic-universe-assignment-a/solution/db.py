"""SQLite schema and connection helper."""
from __future__ import annotations

import sqlite3

SCHEMA = """
-- One row per ingested file. Gives idempotency (file_key + sha256) and per-file counters, so
-- data_quality can be summed without re-reading input.
CREATE TABLE IF NOT EXISTS ingested_files (
    file_key        TEXT PRIMARY KEY,           -- '<drop>/<path inside drop>'
    sha256          TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('leads', 'events')),
    workflow_id     TEXT,
    batch_id        TEXT,
    ingested_at     TEXT NOT NULL,
    rows_total      INTEGER NOT NULL DEFAULT 0,   -- leads: non-blank data rows
    rows_rejected   INTEGER NOT NULL DEFAULT 0,
    dupes_merged    INTEGER NOT NULL DEFAULT 0,
    field_warnings  INTEGER NOT NULL DEFAULT 0,
    events_total    INTEGER NOT NULL DEFAULT 0,   -- events: non-empty lines
    events_malformed INTEGER NOT NULL DEFAULT 0,
    events_duplicate INTEGER NOT NULL DEFAULT 0
);

-- Identity = (workflow_id, phone_hash). No plaintext PII: phone is an HMAC, name+phone are in
-- the encrypted blob. external_ref (policy/order number) is not marked PII by the schema.
CREATE TABLE IF NOT EXISTS leads (
    id           INTEGER PRIMARY KEY,
    workflow_id  TEXT NOT NULL,
    phone_hash   TEXT NOT NULL,
    phone_last4  TEXT NOT NULL,
    batch_id     TEXT NOT NULL,              -- from the first valid row; never changes
    external_ref TEXT NOT NULL,
    attrs        TEXT NOT NULL,              -- JSON of non-PII, non-reference fields
    pii_enc      BLOB NOT NULL,              -- Fernet token of PII fields
    UNIQUE (workflow_id, phone_hash)
);
CREATE INDEX IF NOT EXISTS ix_leads_batch ON leads (workflow_id, batch_id);

CREATE TABLE IF NOT EXISTS lead_rejections (
    id          INTEGER PRIMARY KEY,
    file_key    TEXT NOT NULL REFERENCES ingested_files (file_key),
    workflow_id TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    row_number  INTEGER NOT NULL,
    reason      TEXT NOT NULL
);

-- Append-only interaction ledger. Linking to leads is NOT stored: it is derived at report time
-- by joining on (workflow_id, phone_hash), so late-arriving leads automatically adopt their
-- earlier events and nothing in the ledger ever needs updating.
CREATE TABLE IF NOT EXISTS ledger_events (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL UNIQUE,         -- dedupe key
    event_type      TEXT NOT NULL,
    workflow_id     TEXT,
    call_id         TEXT,
    occurred_at_utc TEXT NOT NULL,                -- fixed-width UTC, sortable
    phone_hash      TEXT,                         -- NULL when absent / invalid
    payload         TEXT NOT NULL,                -- JSON
    file_key        TEXT NOT NULL REFERENCES ingested_files (file_key)
);
CREATE INDEX IF NOT EXISTS ix_events_phone ON ledger_events (workflow_id, phone_hash);
CREATE INDEX IF NOT EXISTS ix_events_call ON ledger_events (workflow_id, call_id);

CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger_events
BEGIN SELECT RAISE(ABORT, 'ledger_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger_events
BEGIN SELECT RAISE(ABORT, 'ledger_events is append-only'); END;
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)  # we manage transactions explicitly
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    return conn
