"""AI classification for the references the rules engine couldn't resolve.

The classifier only ever receives the *sanitized* query, never the raw SQL.
Three backends share one interface:

* ``mock``      - offline keyword heuristic, so the demo runs with no API key
* ``anthropic`` - Claude through the Anthropic API (``ANTHROPIC_API_KEY``)
* ``bedrock``   - Claude through Amazon Bedrock (AWS credentials + ``BEDROCK_MODEL_ID``)

Every backend returns an untrusted dict; ``validator.py`` decides whether to use it.
"""

from __future__ import annotations

import os
import re
from typing import Protocol

SYSTEM_PROMPT = (
    "You are a data classification assistant for a GDPR compliance team. "
    "You receive a SQL query whose literal values have been replaced with '?', "
    "plus the column or table references a rules engine could not classify. "
    "Decide which data classification tags apply to those references, using ONLY "
    "the allowed tags provided. If you are unsure, say so with a low confidence "
    "rather than guessing. Always respond by calling the report_tags tool."
)

REPORT_TOOL = {
    "name": "report_tags",
    "description": "Report the data classification tags for the unresolved references.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tags from the allowed list that apply. Empty if none apply.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "How confident you are that the tags are complete and correct.",
            },
            "rationale": {"type": "string", "description": "One or two sentences explaining why."},
        },
        "required": ["tags", "confidence", "rationale"],
    },
}


def build_prompt(sanitized_sql: str, unknown: list[str], allowed_tags: dict[str, str]) -> str:
    tag_lines = "\n".join(f"- {name}: {desc}" for name, desc in allowed_tags.items())
    unknown_lines = "\n".join(f"- {u}" for u in unknown)
    return (
        f"Sanitized query:\n{sanitized_sql}\n\n"
        f"Unresolved references:\n{unknown_lines}\n\n"
        f"Allowed tags:\n{tag_lines}"
    )


class Classifier(Protocol):
    name: str

    def classify(self, sanitized_sql: str, unknown: list[str], allowed_tags: dict[str, str]) -> dict: ...


class MockClassifier:
    """Keyword heuristic standing in for Claude, so the pipeline runs offline and in CI."""

    name = "mock"
    KEYWORDS = [
        (r"e?mail", "PII_CONTACT", 0.92),
        (r"phone|mobile", "PII_CONTACT", 0.92),
        (r"(first|last|full)_?name|\bname\b", "PII_IDENTITY", 0.85),
        (r"addr|street|postcode|zip", "PII_LOCATION", 0.85),
        (r"iban|bank|card|account_no", "FINANCIAL", 0.88),
        (r"tax_?id|vat", "MERCHANT_RESTRICTED", 0.88),
        (r"cust(omer)?_?(id|ref)|member_?id|user_?id", "PSEUDONYMOUS_ID", 0.8),
    ]

    def classify(self, sanitized_sql: str, unknown: list[str], allowed_tags: dict[str, str]) -> dict:
        tags: set[str] = set()
        confidences: list[float] = []
        unexplained = []
        refs = re.findall(r"column '([\w.]+)'|column ([\w.]+)", " ".join(unknown))
        names = [a or b for a, b in refs]
        for name in names:
            col = name.split(".")[-1]
            hits = [(tag, c) for pat, tag, c in self.KEYWORDS if re.search(pat, col)]
            if hits:
                tags.update(t for t, _ in hits)
                confidences.append(min(c for _, c in hits))
            elif re.search(r"notes?|comment|text|description", col):
                unexplained.append(col)  # free text may contain anything
        if unexplained:
            confidences.append(0.45)
        if not names:
            confidences.append(0.5)  # only an unknown table, no columns to judge
        confidence = min(confidences) if confidences else 0.7
        rationale = f"Keyword match on {', '.join(names) or 'no columns'}"
        if unexplained:
            rationale += f"; free-text fields {', '.join(unexplained)} may hold personal data"
        return {"tags": sorted(tags), "confidence": confidence, "rationale": rationale}


class AnthropicClassifier:
    """Claude via the Anthropic API. Forces a structured answer with tool use."""

    name = "anthropic"

    def __init__(self) -> None:
        import anthropic  # imported lazily so the mock path has no extra dependency

        self.client = anthropic.Anthropic()
        self.model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")

    def classify(self, sanitized_sql: str, unknown: list[str], allowed_tags: dict[str, str]) -> dict:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=[REPORT_TOOL],
            tool_choice={"type": "tool", "name": "report_tags"},
            messages=[{"role": "user", "content": build_prompt(sanitized_sql, unknown, allowed_tags)}],
        )
        for block in response.content:
            if block.type == "tool_use":
                return dict(block.input)
        return {}


class BedrockClassifier:
    """Claude via Amazon Bedrock. The model runs inside AWS; the provider has no access to prompts."""

    name = "bedrock"

    def __init__(self) -> None:
        import boto3

        self.model_id = os.environ.get("BEDROCK_MODEL_ID")
        if not self.model_id:
            raise RuntimeError("Set BEDROCK_MODEL_ID to the Claude model or inference profile ID enabled in your account")
        self.client = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))

    def classify(self, sanitized_sql: str, unknown: list[str], allowed_tags: dict[str, str]) -> dict:
        response = self.client.converse(
            modelId=self.model_id,
            system=[{"text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": build_prompt(sanitized_sql, unknown, allowed_tags)}]}],
            toolConfig={
                "tools": [{"toolSpec": {
                    "name": REPORT_TOOL["name"],
                    "description": REPORT_TOOL["description"],
                    "inputSchema": {"json": REPORT_TOOL["input_schema"]},
                }}],
                "toolChoice": {"tool": {"name": "report_tags"}},
            },
            inferenceConfig={"maxTokens": 1024},
        )
        for item in response["output"]["message"]["content"]:
            if "toolUse" in item:
                return dict(item["toolUse"]["input"])
        return {}


def get_classifier(name: str) -> Classifier:
    backends = {"mock": MockClassifier, "anthropic": AnthropicClassifier, "bedrock": BedrockClassifier}
    if name not in backends:
        raise ValueError(f"unknown classifier '{name}', choose from {sorted(backends)}")
    return backends[name]()
