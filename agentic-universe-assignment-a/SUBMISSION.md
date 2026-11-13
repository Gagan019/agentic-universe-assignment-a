# Submission

## Candidate
- **Name:** Gagan Gowda D
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** 3/4 hours

## How to run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

python -m solution ingest data/drop_1          # DB defaults to ./ledger.db (--db to change)
python -m solution ingest data/drop_2
python -m solution report --out reports/
python -m solution ingest data                 # alternative: every drop_* under a parent, in order
pytest                                         # 45 tests, ~2s
```
PII key: `$AU_PII_KEY`, else a 0600 file `<db>.key` is created next to the DB (git-ignored).
Losing it makes the encrypted names unreadable and breaks phone matching for new data, so in
production it belongs in a secret manager.

## Design
SQLite, 4 tables (`solution/db.py` has the DDL with comments):

- `ingested_files`: one row per file (`'<drop>/<path>'`, sha256) plus that file's counters.
  Gives idempotency, and `data_quality.json` is just a SUM over it.
- `leads`: `UNIQUE(workflow_id, phone_hash)`. `phone_hash` = HMAC-SHA256(key, canonical phone).
  Name + canonical phone live in a Fernet-encrypted blob; non-PII fields in a JSON column.
  Only `phone_last4` is clear (needed for `phone_masked`). `batch_id` is set once.
- `lead_rejections`: `(workflow, batch, row_number, reason)`; no values, so no PII.
- `ledger_events`: append-only (UPDATE/DELETE blocked by triggers), `event_id UNIQUE` is the
  dedupe key, `INSERT OR IGNORE` + rowcount counts duplicates. Phone stored as HMAC only.

**Linking is derived, not stored.** Events are joined to leads at report time on
`(workflow_id, phone_hash)`; overrides go through `call_id` to the lead of the call's other
events. So an event that was an orphan in drop 1 just starts matching once its lead exists in
drop 2: no back-fill, no updates to the ledger, and drop order cannot change the result
(tested: split vs all-at-once give identical files). Orphan counts are therefore always
final-state. Calls, effective dispositions and buckets are computed in `derive.py` with small
pure functions (`effective_disposition`, `lead_bucket`) that the tests hit directly.

Each file is ingested in a single transaction with its `ingested_files` row: a crash leaves
all-or-nothing per file; re-running skips files with the same key+hash.

## Decisions & assumptions
| Decision | Why | Alternative considered |
|---|---|---|
| Phone hash = HMAC with secret key | Plain SHA-256 of a 10-digit number is brute-forceable in seconds | Deterministic encryption; plain hash |
| Names/phone encrypted with Fernet rather than dropped | Ops may need them back; reports don't | Store nothing |
| Re-ingest identity = `drop name/relative path` + sha256; same key with *different* content is refused (error, exit code 2) | Ledger is append-only; silently re-applying a changed file would double count | Replace-on-change |
| `rejections_by_reason` lists only reasons that occurred | Counter-style output | Zero-fill every possible reason |
| `batch_summary` has a row only for batches with >=1 lead | `total_leads` is grouped from leads | Also emit all-rejected batches with zeros |
| Event with unparseable `occurred_at` or missing `event_type` is counted as malformed | Cannot be placed in time; the spec lists only 2 malformed cases | Keep and ignore (never occurs in generated data) |
| Required non-phone field present but invalid -> reject `invalid_value:<field>` | Spec silent; unreachable with the given schemas | Treat like optional |
| Decimal rounding is half-up (Decimal), not float banker's rounding | Money | `round()` |
| Ties on `occurred_at` broken by `event_id` | Deterministic regardless of ingest order | Ingest order |
| Duplicate header columns mapping to one field: first wins | Deterministic | Error |
| Disposition codes matched exactly (case-sensitive) | `RENEWD` is deliberately "unknown" | Case-insensitive |
| Unknown-code override is ignored entirely, so an earlier valid override/set is effective | Spec: "count it and ignore it" | Treat as clearing the disposition |
| Day/month may be 1 or 2 digits (`1/2/2026`) | Harmless leniency | Strict 2-digit |
| `overridden_by` kept in payload | Not marked PII in any schema | Hash it |
| `current_disposition` uses call `initiated_at`, ties by call_id | Spec | n/a |

## Failure modes considered
- Dirty input: all the dirt listed in the README (6 phone formats, ragged/blank rows, BOM,
  renamed/reordered columns, extra columns, bad dates/amounts/enums, malformed/duplicate/
  out-of-order/late events). Non-UTF-8 or non-object JSON lines count as malformed.
- Re-run: file-level skip; event dedupe by `event_id` as a second line of defence.
- Crash mid-file: transaction rollback; rerun picks the file up again.
- Not handled: concurrent `ingest` processes (SQLite single writer would serialize them, but I
  did not test it); a file *edited* after ingest is refused rather than reconciled; key rotation;
  a phone that appears in two workflows is two leads by design; a call whose events point to
  two different leads is attached to the lead of its `call.initiated` (never in the data).

## Testing
`pytest` (45 tests): phone/date/decimal/enum/header/timestamp rules, effective disposition,
bucket precedence, an end-to-end hand-written dataset (blank rows, row numbering, rejection
reasons, merge/last-write-wins, dupes, malformed, orphan vs unknown ordering, override via
call_id, naive-IST timestamps), orphans resolving when the lead arrives later, idempotent
re-ingest, append-only triggers, a byte-level check that no names/phones appear in the DB
files, and generated seeds 42 and 7: drop_1 then drop_2 == all at once.
Not tested: a fault-injected crash mid-file; Postgres.

## Results
Large dataset (seed 42, ~144k lead rows, ~600k events), laptop-class sandbox: ingest of both
drops ~40 s, report ~8 s, ~48 s total. Per-row work is SELECT+INSERT per lead and one INSERT per
event inside per-file transactions. Reports are a full recompute from the ledger each time
(not incremental): simple and always consistent; the cost is O(all events) per report.
Surprises: events for rejected leads (bad phone / missing name / ref) are orphans by design;
the generated `Hindi`/`english`/`tamil` language values are invalid enums -> null + warning ->
default `en`.

## If I had another day
1. Move aggregation into SQL (window functions) and add materialised per-call facts so a report
   after drop N only touches new calls (true incremental).
2. Fault-injection test (kill mid-file) and a concurrency test.
3. Postgres backend + proper migrations; key rotation for the PII key.
4. Production note for 100k leads/day: scheduler per drop, alerts on non-zero `errors`, a
   backfill = re-run `ingest` on a new empty DB and diff reports, since everything is derived.
