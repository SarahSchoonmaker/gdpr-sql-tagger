import json
from pathlib import Path

from gdpr_tagger.pipeline import run
from gdpr_tagger.servicenow import ServiceNowConfig, ServiceNowGRC, issue_payload

ROOT = Path(__file__).parent.parent
CFG = ServiceNowConfig("https://test.service-now.com", "CTRL123", "svc_user", "pw",
                       issue_fields={"assignment_group": "GRC Team"})


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class FakeSession:
    """Stands in for requests.Session and records every call."""

    def __init__(self, open_issue_for=()):
        self.calls = []
        self.open_issue_for = set(open_issue_for)

    def request(self, method, url, timeout=None, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "GET":
            query = kwargs["params"]["sysparm_query"]
            hit = any(t in query for t in self.open_issue_for)
            return FakeResponse({"result": [{"sys_id": "EXISTING"}] if hit else []})
        return FakeResponse({"result": {"sys_id": f"NEW{len(self.calls)}"}})


def _run(tmp_path):
    return run(str(ROOT / "data" / "tickets.json"), str(ROOT / "config" / "tag_catalog.yaml"),
               "mock", str(tmp_path), 0.8)


def _records(tmp_path, summary):
    path = tmp_path / "evidence" / summary["records_file"]
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_live_run_attaches_evidence_to_control_and_opens_review_issues(tmp_path):
    summary = _run(tmp_path)
    session = FakeSession()
    out = ServiceNowGRC(CFG, "live", session=session).publish_run(
        tmp_path / "evidence", summary, _records(tmp_path, summary))

    attachments = [c for c in session.calls if c[1].endswith("/api/now/attachment/file")]
    assert len(attachments) == 2
    assert all(c[2]["params"]["table_sys_id"] == "CTRL123" for c in attachments)
    assert all(c[2]["params"]["table_name"] == "sn_compliance_control" for c in attachments)

    assert sorted(i["ticket_id"] for i in out["issues_created"]) == ["DATA-1006", "DATA-1009"]
    posted = [c[2]["json"] for c in session.calls if c[0] == "POST" and "json" in c[2]]
    assert all(p["assignment_group"] == "GRC Team" for p in posted)


def test_rerun_does_not_duplicate_open_issues(tmp_path):
    summary = _run(tmp_path)
    session = FakeSession(open_issue_for={"DATA-1006"})
    out = ServiceNowGRC(CFG, "live", session=session).publish_run(
        tmp_path / "evidence", summary, _records(tmp_path, summary))
    assert [i["ticket_id"] for i in out["issues_already_open"]] == ["DATA-1006"]
    assert [i["ticket_id"] for i in out["issues_created"]] == ["DATA-1009"]


def test_issue_never_contains_raw_sql(tmp_path):
    summary = _run(tmp_path)
    tickets = {t["id"]: t["sql"] for t in json.loads((ROOT / "data" / "tickets.json").read_text())}
    for rec in _records(tmp_path, summary):
        payload = json.dumps(issue_payload(rec, "run", CFG))
        assert tickets[rec["ticket_id"]] not in payload
        assert "agent_77" not in payload  # literal from DATA-1006


def test_dry_run_calls_nothing_and_writes_requests(tmp_path):
    summary = _run(tmp_path)
    log = tmp_path / "sn.json"
    out = ServiceNowGRC(CFG, "dry-run", dry_run_path=log).publish_run(
        tmp_path / "evidence", summary, _records(tmp_path, summary))
    assert out["mode"] == "dry-run"
    assert len(json.loads(log.read_text())) == 6  # 2 attachments + (lookup + create) x 2 issues
