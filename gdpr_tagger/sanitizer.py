"""Parse SQL and strip anything that could carry real data.

Only the *structure* of a query (tables, columns, joins, filters) survives.
Literal values and comments are removed, because a query like
``WHERE email = 'jane.doe@example.com'`` is itself personal data.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import traverse_scope


class SanitizationError(Exception):
    """The query could not be parsed or could not be proven clean."""


@dataclass(frozen=True)
class ColumnRef:
    """A column the query touches.

    ``table`` is the resolved base table, or None when the query alone can't
    tell which table the column belongs to. ``candidates`` lists the tables
    that could own an unqualified column.
    """

    column: str
    table: str | None = None
    candidates: tuple[str, ...] = ()

    def label(self) -> str:
        if self.table:
            return f"{self.table}.{self.column}"
        if self.candidates:
            return f"{self.column} (one of: {', '.join(self.candidates)})"
        return f"{self.column} (unknown source)"


@dataclass
class SanitizedQuery:
    sanitized_sql: str
    query_sha256: str
    literals_removed: int
    tables: list[str] = field(default_factory=list)
    columns: list[ColumnRef] = field(default_factory=list)


def sanitize(sql: str, dialect: str | None = None) -> SanitizedQuery:
    """Return the query with all literals replaced by ``?`` and comments removed."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except ParseError as e:
        detail = e.errors[0].get("description") if e.errors else str(e)
        raise SanitizationError(f"could not parse SQL: {detail}") from e
    if tree is None:
        raise SanitizationError("empty query")

    removed = 0

    def _strip(node: exp.Expression) -> exp.Expression:
        nonlocal removed
        if isinstance(node, exp.Literal):
            removed += 1
            return exp.Placeholder()
        return node

    clean = tree.transform(_strip)
    sanitized_sql = clean.sql(dialect=dialect, comments=False)
    _assert_no_literals(sanitized_sql, dialect)

    tables, columns = _extract_refs(clean)
    return SanitizedQuery(
        sanitized_sql=sanitized_sql,
        # A hash links evidence back to the original ticket without storing the raw query.
        query_sha256=hashlib.sha256(sql.encode()).hexdigest(),
        literals_removed=removed,
        tables=tables,
        columns=columns,
    )


def _assert_no_literals(sanitized_sql: str, dialect: str | None) -> None:
    """Defense in depth: re-parse the output and prove nothing literal survived."""
    reparsed = sqlglot.parse_one(sanitized_sql, read=dialect)
    leftovers = list(reparsed.find_all(exp.Literal))
    if leftovers or "--" in sanitized_sql or "/*" in sanitized_sql:
        raise SanitizationError("sanitized query still contains literals or comments")


def _extract_refs(tree: exp.Expression) -> tuple[list[str], list[ColumnRef]]:
    """Resolve every column to its base table, scope by scope (handles aliases and subqueries)."""
    tables: set[str] = set()
    refs: set[ColumnRef] = set()

    for scope in traverse_scope(tree):
        base_tables = {
            alias: src.name.lower()
            for alias, src in scope.sources.items()
            if isinstance(src, exp.Table)
        }
        tables.update(base_tables.values())

        for col in scope.columns:
            name = col.name.lower()
            if col.table:
                if col.table in base_tables:
                    refs.add(ColumnRef(name, base_tables[col.table]))
                elif col.table not in scope.sources:
                    refs.add(ColumnRef(name))
                # else: it points at a derived table; the inner scope resolves it.
            elif len(scope.sources) == 1:
                if base_tables:
                    refs.add(ColumnRef(name, next(iter(base_tables.values()))))
            else:
                refs.add(ColumnRef(name, None, tuple(sorted(set(base_tables.values())))))

        # SELECT * and SELECT t.* touch every column of the table.
        if isinstance(scope.expression, exp.Select):
            for proj in scope.expression.expressions:
                if isinstance(proj, exp.Star):
                    refs.update(ColumnRef("*", t) for t in base_tables.values())
                elif isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star):
                    if proj.table in base_tables:
                        refs.add(ColumnRef("*", base_tables[proj.table]))

    ordered = sorted(refs, key=lambda r: (r.table or "~", r.column))
    return sorted(tables), ordered
