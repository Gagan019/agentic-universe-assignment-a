"""Loading of workflow lead schemas and the disposition catalogue."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "assignment-a-data" / "schemas"


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    values: tuple[str, ...] = ()
    default: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Schema:
    workflow_id: str
    fields: tuple[Field, ...]

    @property
    def reference_field(self) -> Field:
        return next(f for f in self.fields if f.reference)


@dataclass
class Catalog:
    schemas: dict[str, Schema] = field(default_factory=dict)
    # (workflow_id, code) -> (is_terminal, is_success)
    dispositions: dict[tuple[str, str], tuple[bool, bool]] = field(default_factory=dict)


def load_catalog(schema_dir: Path | str | None = None) -> Catalog:
    d = Path(schema_dir) if schema_dir else DEFAULT_SCHEMA_DIR
    cat = Catalog()
    for p in sorted(d.glob("*.json")):
        raw = json.loads(p.read_text(encoding="utf-8"))
        fields = tuple(Field(
            name=f["name"], type=f["type"], required=f.get("required", False),
            pii=f.get("pii", False), reference=f.get("reference", False),
            values=tuple(f.get("values", ())), default=f.get("default"),
            aliases=tuple(f.get("aliases", ()))) for f in raw["fields"])
        cat.schemas[raw["workflow_id"]] = Schema(raw["workflow_id"], fields)
    with open(d / "dispositions.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            cat.dispositions[(row["workflow_id"], row["code"])] = (
                row["is_terminal"].strip().lower() == "true",
                row["is_success"].strip().lower() == "true")
    return cat
