import json
import re
from pathlib import Path

import sqlglot
from sqlglot import exp

from gdpr_tagger.classifier import MockClassifier
from gdpr_tagger.pipeline import AUTO_AI, AUTO_RULES, REVIEW, process_ticket, run
from gdpr_tagger.rules import load_catalog

ROOT = Path(__file__).parent.parent
CATALOG = load_catalog(ROOT / "config" / "tag_catalog.yaml")
TICKETS = json.loads((ROOT / "data" / "tickets.json").read_text())

EXPECTED = {
    "DATA-1001": AUTO_RULES,   # catalog columns via aliases and a join
    "DATA-1002": AUTO_RULES,   # no personal data
    "DATA-1003": AUTO_RULES,   # restricted merchant data
    "DATA-1004": AUTO_RULES,   # SELECT * expanded from the catalog
    "DATA-1005": AUTO_AI,      # unknown table, clear column name -> AI
    "DATA-1006": REVIEW,       # unknown table with free-text notes -> low confidence
    "DATA-1007": AUTO_RULES,
    "DATA-1008": AUTO_RULES,   # subquery
    "DATA-1009": REVIEW,       # unparseable
    "DATA-1010": AUTO_RULES,
}


def test_each_sample_ticket_gets_the_expected_decision():
    for ticket in TICKETS:
        rec = process_ticket(ticket, CATALOG, MockClassifier(), threshold=0.8)
        assert rec["decision"] == EXPECTED[ticket["id"]], ticket["id"]


class SpyClassifier(MockClassifier):
    """Records exactly what would be sent to the model."""

    def __init__(self):
        self.sent = []

    def classify(self, sanitized_sql, unknown, allowed_tags):
        self.sent.append(sanitized_sql + " " + " ".join(unknown))
        return super().classify(sanitized_sql, unknown, allowed_tags)


def _literal_values(sql):
    try:
        tree = sqlglot.parse_one(sql)
    except sqlglot.errors.ParseError:
        return []
    return [lit.this for lit in tree.find_all(exp.Literal) if len(lit.this) >= 2]


def test_no_literal_value_reaches_the_model_or_the_evidence():
    reached_model = 0
    for t in TICKETS:
        spy = SpyClassifier()
        rec = process_ticket(t, CATALOG, spy, 0.8)
        evidence_text = json.dumps(rec)
        model_text = " ".join(spy.sent)
        reached_model += bool(spy.sent)
        assert t["sql"] not in evidence_text, t["id"]
        for value in _literal_values(t["sql"]):
            assert not re.search(rf"\b{re.escape(value)}\b", model_text), (t["id"], value)
            assert not re.search(rf"\b{re.escape(value)}\b", rec.get("sanitized_sql", "")), (t["id"], value)
    assert reached_model >= 2, "expected some tickets to exercise the classifier path"


def test_model_failure_never_blocks_the_pipeline():
    class Down(MockClassifier):
        def classify(self, *a):
            raise TimeoutError("model unavailable")

    ticket = next(t for t in TICKETS if t["id"] == "DATA-1005")
    rec = process_ticket(ticket, CATALOG, Down(), 0.8)
    assert rec["decision"] == REVIEW
    assert "unavailable" in rec["classifier"]["error"]


def test_full_run_writes_complete_evidence(tmp_path):
    summary = run(str(ROOT / "data" / "tickets.json"), str(ROOT / "config" / "tag_catalog.yaml"),
                  "mock", str(tmp_path), 0.8)
    assert summary["complete"] and summary["population_processed"] == len(TICKETS)
    lines = (tmp_path / "evidence" / summary["records_file"]).read_text().splitlines()
    assert len(lines) == len(TICKETS)
    tagged = json.loads((tmp_path / "tickets_tagged.json").read_text())
    assert all("review_status" in t for t in tagged)
