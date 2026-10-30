"""Ticketing and evidence integrations.

``LocalTicketStore`` reads and writes JSON so the demo runs anywhere. In a real
deployment you would swap it for a Jira or ServiceNow client with the same
two methods (``load`` and ``apply``).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


class LocalTicketStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.tickets: list[dict] = []

    def load(self) -> list[dict]:
        self.tickets = json.loads(self.path.read_text())
        return self.tickets

    def apply(self, ticket_id: str, tags: list[str], suggested: list[str], status: str, note: str) -> None:
        for t in self.tickets:
            if t["id"] == ticket_id:
                t.update(gdpr_tags=tags, suggested_tags=suggested, review_status=status, tagging_note=note)
                return
        raise KeyError(ticket_id)

    def save(self, out_path: str | Path) -> None:
        Path(out_path).write_text(json.dumps(self.tickets, indent=2) + "\n")


class EvidenceLog:
    """Append-only, timestamped record of every decision. Never stores raw SQL.

    This is the "information produced by the entity" an auditor would test, so
    each run records what it ran (code version, catalog hash, settings) and a
    population count they can reconcile against the ticket source.
    """

    def __init__(self, out_dir: str | Path, meta: dict) -> None:
        self.started = datetime.now(timezone.utc)
        self.run_id = self.started.strftime("%Y%m%dT%H%M%SZ")
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.records_path = self.dir / f"evidence-{self.run_id}.jsonl"
        self.meta = {
            "run_id": self.run_id,
            "started_at": self.started.isoformat(),
            "code_version": os.environ.get("GITHUB_SHA", "local"),
            "triggered_by": os.environ.get("GITHUB_EVENT_NAME", "manual"),
            **meta,
        }
        self.records: list[dict] = []

    def record(self, entry: dict) -> None:
        entry = {"logged_at": datetime.now(timezone.utc).isoformat(), **entry}
        self.records.append(entry)
        with self.records_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    def finish(self, population: int) -> dict:
        decisions: dict[str, int] = {}
        for r in self.records:
            decisions[r["decision"]] = decisions.get(r["decision"], 0) + 1
        summary = {
            **self.meta,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "population_in_source": population,
            "population_processed": len(self.records),
            "complete": population == len(self.records),
            "decisions": decisions,
            "records_file": self.records_path.name,
        }
        (self.dir / f"summary-{self.run_id}.json").write_text(json.dumps(summary, indent=2) + "\n")
        return summary
