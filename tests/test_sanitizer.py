import pytest

from gdpr_tagger.sanitizer import ColumnRef, SanitizationError, sanitize


def test_literals_and_comments_are_removed():
    q = sanitize("SELECT email FROM customers WHERE email = 'jane.doe@example.com' AND id = 42 -- check Jane")
    assert "jane" not in q.sanitized_sql.lower()
    assert "42" not in q.sanitized_sql
    assert "--" not in q.sanitized_sql
    assert q.literals_removed == 2
    assert q.sanitized_sql.count("?") == 2


def test_structure_is_preserved():
    q = sanitize("SELECT c.email FROM customers c JOIN orders o ON c.id = o.customer_id WHERE o.total > 100")
    assert q.tables == ["customers", "orders"]
    assert ColumnRef("email", "customers") in q.columns
    assert ColumnRef("customer_id", "orders") in q.columns


def test_aliases_resolve_to_base_tables():
    q = sanitize("SELECT x.email FROM customers AS x")
    assert q.columns == [ColumnRef("email", "customers")]


def test_subquery_columns_resolve_in_inner_scope():
    q = sanitize("SELECT r.e FROM (SELECT email AS e FROM customers) r")
    assert ColumnRef("email", "customers") in q.columns


def test_star_is_expanded_per_table():
    q = sanitize("SELECT c.* FROM customers c JOIN orders o ON c.id = o.customer_id")
    assert ColumnRef("*", "customers") in q.columns
    assert ColumnRef("*", "orders") not in q.columns


def test_unqualified_column_with_two_tables_lists_candidates():
    q = sanitize("SELECT email FROM customers c JOIN orders o ON c.id = o.customer_id")
    assert ColumnRef("email", None, ("customers", "orders")) in q.columns


def test_hash_identifies_original_without_storing_it():
    a, b = sanitize("SELECT 1 FROM t WHERE x = 'a'"), sanitize("SELECT 1 FROM t WHERE x = 'b'")
    assert a.sanitized_sql == b.sanitized_sql
    assert a.query_sha256 != b.query_sha256


def test_unparseable_sql_raises():
    with pytest.raises(SanitizationError):
        sanitize("SELEC email FRM customers WHERE")
