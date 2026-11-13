# AI Usage Disclosure

## Tools used
Claude (Anthropic), used in a chat session that could run code in a sandbox.

## What I used them for
I picked Assignment A and gave Claude the repository and the task text. Claude wrote the first
version of the whole `solution/` package (value normalisation, schema and database design,
lead and event ingestion, derived metrics, report writers and the CLI), the tests, and the
first draft of SUBMISSION.md. It also generated the sample data with the provided generator
and ran the timings reported in SUBMISSION.md.

## What I wrote or decided myself
I chose Assignment A over B and chose to submit Claude's design. The design decisions came
from Claude: linking events to leads at report time instead of storing the link, hashing phones
with a secret key and encrypting names, and loading each file in one transaction with a file
hash for idempotency. So did every row of the "Decisions & assumptions" table. I did not write
the code line by line. [AFTER YOU READ THE CODE: add one or two real sentences here, for
example "I changed X to Y because Z", or "I traced how an orphan event gets linked once its lead
arrives in drop 2".]

## Where the AI was wrong or unhelpful
- One test assertion for orphan events was written loosely (`== X or >= 4`), so it could not
  fail in a useful way. It was caught on review and tightened to the exact count (6).
- Claude could not see the grader's expected output, so some formatting choices are guesses,
  for example which reasons appear in `rejections_by_reason` and whether a batch with no valid
  leads gets a row. These are listed in the decisions table as open calls.

## How I verified AI-generated code
- Ran `pytest` (45 tests covering the rule functions, an end-to-end hand-written dataset,
  re-ingesting the same data, the append-only ledger, and a byte-level check that no names or
  phone numbers appear in the database files).
- Ran the pipeline on generated seeds 42 and 7 (small) and 42 (large), and checked that
  ingesting drop 1 then drop 2 produces the same reports as ingesting everything at once.
- Compared sample output with the examples in the assignment README (for example `0.5625`).