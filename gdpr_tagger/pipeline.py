"""End-to-end pipeline: sanitize -> rules -> (AI if needed) -> validate -> tag ticket -> log evidence."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .classifier import Classifier, get_classifier
from .integrations import EvidenceLog, LocalTicketStore
from .rules import Catalog, apply_rules, load_catalog
from .sanitizer import SanitizationError, sanitize
from .validator import validate

AUTO_RULES = "auto_tagged_rules"
AUTO_AI = "auto_tagged_ai"
REVIEW = "needs_human_review"


def process_ticket(ticket: dict, catalog: Catalog, classifier: Classifier, threshold: float) -> dict:
    """Return the decision record for one ticket. Pure function apart from the classifier call."""
    record: dict = {"ticket_id": ticket["id"]}

    # 1. Sanitize. Nothing downstream ever sees the raw SQL.
    try:
        q = sanitize(ticket["sql"])
    except SanitizationError as e:
        return {**record, "decision": REVIEW, "final_tags": [], "suggested_tags": [],
                "note": f"Sanitizer rejected query: {e}"}
    record.update(query_sha256=q.query_sha256, sanitized_sql=q.sanitized_sql,
                  literals_removed=q.literals_removed)

    # 2. Deterministic rules against the catalog.
    rules = apply_rules(q, catalog)
    record.update(rule_matches=rules.matches, unresolved=rules.unknown)

    if not rules.unknown:
        tags = catalog.apply_derived(rules.tags) or {"NO_PERSONAL_DATA"}
        return {**record, "decision": AUTO_RULES, "final_tags": sorted(tags), "suggested_tags": [],
                "note": "All references resolved by catalog rules"}

    # 3. AI only for what rules couldn't settle. Sanitized SQL only.
    try:
        raw = classifier.classify(q.sanitized_sql, rules.unknown, catalog.allowed_tags)
    except Exception as e:  # the pipeline never blocks on the model
        return {**record, "decision": REVIEW, "final_tags": sorted(catalog.apply_derived(rules.tags)),
                "suggested_tags": [], "classifier": {"name": classifier.name, "error": str(e)},
                "note": "Classifier unavailable; rule tags applied, unresolved refs need review"}

    # 4. Validate before trusting anything the model said.
    v = validate(raw, catalog.allowed_tags, threshold)
    record["classifier"] = {"name": classifier.name, "raw_output": raw, "confidence": v.confidence,
                            "rationale": v.rationale, "problems": v.problems}

    if v.needs_review:
        return {**record, "decision": REVIEW,
                "final_tags": sorted(catalog.apply_derived(rules.tags)),
                "suggested_tags": sorted(v.tags - rules.tags),
                "note": "; ".join(v.problems)}

    tags = catalog.apply_derived(rules.tags | v.tags) or {"NO_PERSONAL_DATA"}
    return {**record, "decision": AUTO_AI, "final_tags": sorted(tags), "suggested_tags": [],
            "note": v.rationale}


def run(tickets_path: str, catalog_path: str, classifier_name: str, out_dir: str, threshold: float) -> dict:
    catalog = load_catalog(catalog_path)
    classifier = get_classifier(classifier_name)
    store = LocalTicketStore(tickets_path)
    tickets = store.load()
    evidence = EvidenceLog(Path(out_dir) / "evidence", {
        "classifier": classifier.name,
        "confidence_threshold": threshold,
        "catalog_sha256": catalog.sha256,
        "ticket_source": str(tickets_path),
    })

    for ticket in tickets:
        rec = process_ticket(ticket, catalog, classifier, threshold)
        status = "needs_human_review" if rec["decision"] == REVIEW else "auto_tagged"
        store.apply(ticket["id"], rec["final_tags"], rec["suggested_tags"], status, rec["note"])
        evidence.record(rec)
        _print_row(rec)

    store.save(Path(out_dir) / "tickets_tagged.json")
    summary = evidence.finish(population=len(tickets))
    print(f"\nProcessed {summary['population_processed']}/{summary['population_in_source']} tickets "
          f"(complete={summary['complete']}): {summary['decisions']}")
    print(f"Evidence: {evidence.dir}/{summary['records_file']}")
    return summary


def _print_row(rec: dict) -> None:
    tags = ", ".join(rec["final_tags"]) or "-"
    extra = f"  [suggested: {', '.join(rec['suggested_tags'])}]" if rec["suggested_tags"] else ""
    print(f"{rec['ticket_id']:<10} {rec['decision']:<20} {tags}{extra}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Tag SQL tickets with GDPR data classifications.")
    p.add_argument("--tickets", default="data/tickets.json")
    p.add_argument("--catalog", default="config/tag_catalog.yaml")
    p.add_argument("--classifier", default="mock", choices=["mock", "anthropic", "bedrock"])
    p.add_argument("--out", default="output")
    p.add_argument("--threshold", type=float, default=0.8, help="minimum AI confidence to auto-tag")
    args = p.parse_args(argv)

    summary = run(args.tickets, args.catalog, args.classifier, args.out, args.threshold)
    return 0 if summary["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
