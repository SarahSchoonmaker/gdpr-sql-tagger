from pathlib import Path

import pytest

from gdpr_tagger.rules import apply_rules, load_catalog
from gdpr_tagger.sanitizer import sanitize
from gdpr_tagger.validator import validate

CATALOG = load_catalog(Path(__file__).parent.parent / "config" / "tag_catalog.yaml")


def rules_for(sql):
    return apply_rules(sanitize(sql), CATALOG)


def test_catalog_columns_are_tagged():
    r = rules_for("SELECT email, full_name FROM customers")
    assert r.tags == {"PII_CONTACT", "PII_IDENTITY"}
    assert not r.unknown


def test_derived_tags_are_added():
    assert "GDPR_PERSONAL_DATA" in CATALOG.apply_derived({"PII_CONTACT"})
    assert "RESTRICTED_ACCESS_REVIEW" in CATALOG.apply_derived({"MERCHANT_RESTRICTED"})
    assert CATALOG.apply_derived({"GEO_REGION"}) == {"GEO_REGION"}


def test_unqualified_column_resolved_when_only_one_table_owns_it():
    r = rules_for("SELECT email FROM customers c JOIN orders o ON c.id = o.customer_id")
    assert "PII_CONTACT" in r.tags
    assert not r.unknown


def test_unknown_table_and_column_are_reported():
    r = rules_for("SELECT member_email FROM loyalty_profiles")
    assert any("loyalty_profiles" in u for u in r.unknown)
    assert any("member_email" in u for u in r.unknown)
    r = rules_for("SELECT favourite_colour FROM customers")
    assert r.unknown == ["column 'customers.favourite_colour' is not in the catalog"]


def test_catalog_rejects_tags_not_in_allowed_list(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("allowed_tags: {A: a}\ntables: {t: {c: [B]}}\n")
    with pytest.raises(ValueError):
        load_catalog(bad)


ALLOWED = CATALOG.allowed_tags


def test_valid_confident_output_is_accepted():
    v = validate({"tags": ["PII_CONTACT"], "confidence": 0.95, "rationale": "email column"}, ALLOWED, 0.8)
    assert v.tags == {"PII_CONTACT"} and not v.needs_review


def test_invented_tags_are_dropped_and_flagged():
    v = validate({"tags": ["PII_CONTACT", "SUPER_SECRET"], "confidence": 0.99, "rationale": "x"}, ALLOWED, 0.8)
    assert v.tags == {"PII_CONTACT"}
    assert v.needs_review


def test_low_confidence_goes_to_review():
    v = validate({"tags": [], "confidence": 0.3, "rationale": "unsure"}, ALLOWED, 0.8)
    assert v.needs_review


@pytest.mark.parametrize("raw", [{}, {"tags": "PII_CONTACT", "confidence": 0.9, "rationale": "x"},
                                 {"tags": [], "confidence": 7, "rationale": "x"}])
def test_malformed_output_goes_to_review(raw):
    assert validate(raw, ALLOWED, 0.8).needs_review
