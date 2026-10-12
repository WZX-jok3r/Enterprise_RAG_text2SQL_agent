"""Strict, versioned contracts for answer-quality evaluation."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GoldEvidence(StrictModel):
    source: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class QualityCase(StrictModel):
    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    category: str = Field(min_length=1)
    answerable: bool
    reference_answer: str = Field(min_length=1)
    gold_evidence: list[GoldEvidence] = Field(default_factory=list)
    required_facts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def evidence_required(self):
        if self.answerable and not self.gold_evidence:
            raise ValueError("Answerable cases require annotated evidence")
        return self


class Context(StrictModel):
    id: int = Field(ge=1)
    source: str = Field(min_length=1)
    text: str = Field(min_length=1)


class ResponseRecord(StrictModel):
    id: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    contexts: list[Context] = Field(default_factory=list)
    error: str | None = None

    @model_validator(mode="after")
    def unique_context_ids(self):
        ids = [c.id for c in self.contexts]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate context IDs")
        return self


class Claim(StrictModel):
    text: str = Field(min_length=1)
    verdict: Literal["supported", "contradicted", "unsupported"]
    evidence_ids: list[int] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class CitationReview(StrictModel):
    context_id: int = Field(ge=1)
    supported: bool
    reason: str = Field(min_length=1)


class QualityVerdict(StrictModel):
    correctness: Literal["correct", "partial", "incorrect"]
    refusal: Literal["none", "appropriate", "unnecessary"]
    claims: list[Claim]
    citations: list[CitationReview]
    reason: str = Field(min_length=1)
