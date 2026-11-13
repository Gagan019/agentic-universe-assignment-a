"""CLI:  python -m solution [--db ledger.db] ingest <drop-or-parent-dir>
        python -m solution [--db ledger.db] report --out reports/
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import db as dbmod
from .ingest import ingest_path
from .pii import Pii, load_master_key
from .report import write_reports
from .schemas import load_catalog


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m solution")
    ap.add_argument("--db", default="ledger.db", help="SQLite file (default: ledger.db)")
    ap.add_argument("--schemas", default=None, help="schema dir (default: assignment-a-data/schemas)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_in = sub.add_parser("ingest", help="ingest a drop dir (or a parent holding drop_* dirs)")
    p_in.add_argument("path")
    p_rep = sub.add_parser("report", help="write the five report files")
    p_rep.add_argument("--out", default="reports")
    args = ap.parse_args(argv)

    catalog = load_catalog(args.schemas)
    conn = dbmod.connect(args.db)
    try:
        if args.cmd == "ingest":
            pii = Pii(load_master_key(args.db))
            rep = ingest_path(conn, pii, catalog, Path(args.path))
            print(f"done: {rep.ingested} ingested, {rep.skipped} skipped, {rep.errors} errors")
            return 2 if rep.errors else 0
        write_reports(conn, catalog, args.out)
        print(f"reports written to {args.out}/")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
