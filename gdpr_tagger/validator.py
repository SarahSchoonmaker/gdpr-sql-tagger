"""Guardrails on AI output. Model answers are untrusted until they pass here."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field, ValidationError


class ClassifierOutput(BaseModel):
    tags: list[str]
    confidence: float = Field(ge=0, le=1)
    rationale: str


@dataclass
class ValidationResult:
    tags: set[str] = field(default_factory=set)
    confidence: float = 0.0
    rationale: str = ""
    problems: list[str] = field(default_factory=list)
    needs_review: bool = False


def validate(raw: dict, allowed_tags: dict[str, str], threshold: float) -> ValidationResult:
    try:
        parsed = ClassifierOutput.model_validate(raw)
    except ValidationError as e:
        return ValidationResult(problems=[f"malformed output: {err['loc']} {err['msg']}" for err in e.errors()],
                                needs_review=True)

    result = ValidationResult(confidence=parsed.confidence, rationale=parsed.rationale)

    invalid = [t for t in parsed.tags if t not in allowed_tags]
    if invalid:
        # Never apply a tag the model invented. Keep the valid ones as suggestions.
        result.problems.append(f"tags not in allowed list were dropped: {invalid}")
        result.needs_review = True
    result.tags = {t for t in parsed.tags if t in allowed_tags}

    if parsed.confidence < threshold:
        result.problems.append(f"confidence {parsed.confidence:.2f} is below threshold {threshold:.2f}")
        result.needs_review = True

    return result
