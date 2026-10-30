"""Deterministic tagging against the data classification catalog.

Rules run first. They are predictable, cheap, and easy for an auditor to
re-perform. Only references the catalog can't resolve go to the AI step.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .sanitizer import SanitizedQuery


@dataclass
class Catalog:
    allowed_tags: dict[str, str]
    derived_tags: dict[str, list[str]]
    tables: dict[str, dict[str, list[str]]]
    sha256: str

    def apply_derived(self, tags: set[str]) -> set[str]:
        out = set(tags)
        for derived, triggers in self.derived_tags.items():
            if out & set(triggers):
                out.add(derived)
        return out


def load_catalog(path: str | Path) -> Catalog:
    raw = Path(path).read_bytes()
    data = yaml.safe_load(raw)
    tables = {
        t.lower(): {c.lower(): list(tags or []) for c, tags in cols.items()}
        for t, cols in data["tables"].items()
    }
    derived = {k: v["when_any"] for k, v in (data.get("derived_tags") or {}).items()}
    catalog = Catalog(data["allowed_tags"], derived, tables, hashlib.sha256(raw).hexdigest())

    unknown = {t for cols in tables.values() for tags in cols.values() for t in tags}
    unknown |= {t for trig in derived.values() for t in trig}
    unknown -= set(catalog.allowed_tags)
    if unknown:
        raise ValueError(f"catalog uses tags that are not in allowed_tags: {sorted(unknown)}")
    return catalog


@dataclass
class RuleResult:
    tags: set[str] = field(default_factory=set)
    matches: list[dict] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def apply_rules(query: SanitizedQuery, catalog: Catalog) -> RuleResult:
    result = RuleResult()

    for table in query.tables:
        if table not in catalog.tables:
            result.unknown.append(f"table '{table}' is not in the catalog")

    for ref in query.columns:
        table = ref.table
        if table is None:
            owners = [t for t in ref.candidates if ref.column in catalog.tables.get(t, {})]
            if len(owners) == 1:
                table = owners[0]
            else:
                result.unknown.append(f"cannot resolve column {ref.label()}")
                continue

        if table not in catalog.tables:
            label = "*" if ref.column == "*" else ref.column
            result.unknown.append(f"column '{table}.{label}' (table not in catalog)")
            continue

        columns = catalog.tables[table]
        if ref.column == "*":
            tags = {t for col_tags in columns.values() for t in col_tags}
            result.tags |= tags
            result.matches.append({"ref": f"{table}.*", "tags": sorted(tags), "rule": "star_expansion"})
        elif ref.column in columns:
            tags = set(columns[ref.column])
            result.tags |= tags
            result.matches.append({"ref": f"{table}.{ref.column}", "tags": sorted(tags), "rule": "catalog"})
        else:
            result.unknown.append(f"column '{table}.{ref.column}' is not in the catalog")

    return result
