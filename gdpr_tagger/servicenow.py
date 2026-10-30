"""Push evidence and review items to ServiceNow GRC (Policy and Compliance).

Each run does two things:

1. Attaches the evidence file and run summary to the GRC control record the
   pipeline supports (table ``sn_compliance_control``), so the control carries
   its own continuous evidence.
2. Opens a GRC issue (table ``sn_grc_issue``) for every ticket that needs human
   review, unless an issue for the same ticket is already open.

Modes:

* ``dry-run`` writes the exact API requests to a JSON file and calls nothing.
  Use it to demo or to check payloads before going live.
* ``live`` calls the ServiceNow REST API (Attachment API and Table API).

Table and field names follow ServiceNow's standard GRC data model but can vary
by instance and version, so they are configurable. Confirm them with your
ServiceNow admin before running live.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

REVIEW = "needs_human_review"


@dataclass
class ServiceNowConfig:
    instance_url: str               # e.g. https://yourcompany.service-now.com
    control_sys_id: str             # sys_id of the GRC control this pipeline evidences
    user: str = ""
    password: str = ""
    control_table: str = "sn_compliance_control"
    issue_table: str = "sn_grc_issue"
    issue_fields: dict = field(default_factory=dict)  # extra fields, e.g. assignment group

    @classmethod
    def from_env(cls) -> "ServiceNowConfig":
        missing = [v for v in ("SN_INSTANCE_URL", "SN_CONTROL_SYS_ID") if not os.environ.get(v)]
        if missing:
            raise RuntimeError(f"missing environment variables: {', '.join(missing)}")
        return cls(
            instance_url=os.environ["SN_INSTANCE_URL"].rstrip("/"),
            control_sys_id=os.environ["SN_CONTROL_SYS_ID"],
            user=os.environ.get("SN_USER", ""),
            password=os.environ.get("SN_PASSWORD", ""),
            control_table=os.environ.get("SN_CONTROL_TABLE", "sn_compliance_control"),
            issue_table=os.environ.get("SN_ISSUE_TABLE", "sn_grc_issue"),
            issue_fields=json.loads(os.environ.get("SN_ISSUE_FIELDS", "{}")),
        )


def issue_payload(rec: dict, run_id: str, cfg: ServiceNowConfig) -> dict:
    """Build a GRC issue from an evidence record. Uses sanitized SQL only, never the raw query."""
    lines = [
        f"Ticket {rec['ticket_id']} needs human review of its GDPR data classification.",
        f"Reason: {rec.get('note', '')}",
        f"Rule tags applied: {', '.join(rec.get('final_tags', [])) or 'none'}",
        f"AI-suggested tags: {', '.join(rec.get('suggested_tags', [])) or 'none'}",
        f"Unresolved references: {'; '.join(rec.get('unresolved', [])) or 'none'}",
        f"Sanitized query: {rec.get('sanitized_sql', '(query could not be parsed)')}",
        f"Query SHA-256: {rec.get('query_sha256', 'n/a')}",
        f"Pipeline run: {run_id}",
    ]
    return {
        "short_description": f"[GDPR tagging] Review required: {rec['ticket_id']}",
        "description": "\n".join(lines),
        **cfg.issue_fields,
    }


class ServiceNowGRC:
    def __init__(self, cfg: ServiceNowConfig, mode: str = "dry-run", session=None, dry_run_path: Path | None = None):
        if mode not in ("dry-run", "live"):
            raise ValueError("mode must be 'dry-run' or 'live'")
        self.cfg, self.mode = cfg, mode
        self.dry_run_path = dry_run_path
        self.requests_log: list[dict] = []
        self.session = session
        if mode == "live" and session is None:
            import requests

            self.session = requests.Session()
            self.session.auth = (cfg.user, cfg.password)
            self.session.headers.update({"Accept": "application/json"})

    # --- low-level -------------------------------------------------------

    def _call(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.cfg.instance_url}{path}"
        logged = {"method": method, "url": url, "params": kwargs.get("params")}
        if "json" in kwargs:
            logged["json"] = kwargs["json"]
        if "data" in kwargs:
            logged["body"] = f"<{len(kwargs['data'])} bytes>"
        self.requests_log.append(logged)
        if self.mode == "dry-run":
            return {"result": []} if method == "GET" else {"result": {"sys_id": "DRY-RUN"}}
        resp = self.session.request(method, url, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp.json()

    # --- operations -------------------------------------------------------

    def attach_file(self, path: Path) -> str:
        """Attach a file to the control record via the Attachment API."""
        result = self._call(
            "POST", "/api/now/attachment/file",
            params={"table_name": self.cfg.control_table, "table_sys_id": self.cfg.control_sys_id,
                    "file_name": path.name},
            headers={"Content-Type": "application/json" if path.suffix == ".json" else "application/x-ndjson"},
            data=path.read_bytes(),
        )
        return result["result"]["sys_id"]

    def find_open_issue(self, ticket_id: str) -> str | None:
        result = self._call(
            "GET", f"/api/now/table/{self.cfg.issue_table}",
            params={"sysparm_query": f"short_descriptionLIKE{ticket_id}^active=true",
                    "sysparm_fields": "sys_id", "sysparm_limit": 1},
        )
        rows = result.get("result") or []
        return rows[0]["sys_id"] if rows else None

    def create_issue(self, payload: dict) -> str:
        result = self._call("POST", f"/api/now/table/{self.cfg.issue_table}", json=payload)
        return result["result"]["sys_id"]

    # --- one call per pipeline run -----------------------------------------

    def publish_run(self, evidence_dir: Path, summary: dict, records: list[dict]) -> dict:
        out = {"mode": self.mode, "attachments": [], "issues_created": [], "issues_already_open": []}
        for name in (summary["records_file"], f"summary-{summary['run_id']}.json"):
            out["attachments"].append({"file": name, "sys_id": self.attach_file(evidence_dir / name)})

        for rec in records:
            if rec["decision"] != REVIEW:
                continue
            existing = self.find_open_issue(rec["ticket_id"])
            if existing:  # idempotent: re-runs don't duplicate issues
                out["issues_already_open"].append({"ticket_id": rec["ticket_id"], "sys_id": existing})
                continue
            sys_id = self.create_issue(issue_payload(rec, summary["run_id"], self.cfg))
            out["issues_created"].append({"ticket_id": rec["ticket_id"], "sys_id": sys_id})

        if self.mode == "dry-run" and self.dry_run_path:
            self.dry_run_path.write_text(json.dumps(self.requests_log, indent=2) + "\n")
        return out
