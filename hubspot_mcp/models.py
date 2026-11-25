"""Strict argument models: no coercion, no extra fields. The advertised schemas come from these."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Callable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .common import check_text

ContactId = Annotated[str, Field(pattern=r"^[0-9]{1,20}$", description="ID returned by search_new_leads")]


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, frozen=True)

    def log_args(self, secret: Callable[[str], Any]) -> dict[str, Any]:
        """What the audit log may hold: IDs, dates and in-range integers raw; text only as HMAC."""
        return {}


class SearchArgs(Args):
    days: Annotated[int, Field(ge=1, le=30, description="How many days back to look")]

    def log_args(self, secret):
        return {"days": self.days}


class ContactArgs(Args):
    contact_id: ContactId

    def log_args(self, secret):
        return {"contact_id": self.contact_id}


class ScoreArgs(Args):
    contact_id: ContactId
    score: Annotated[int, Field(ge=0, le=100)]
    reasons: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]],
                       Field(min_length=1, max_length=3, description="Short plain-text reasons, no links")]
    confidence: Annotated[float, Field(ge=0, le=1)]
    source_ids: Annotated[list[Annotated[str, Field(pattern=r"^src_[0-9a-f]{12}$")]],
                          Field(max_length=5, description="Source IDs from research_company. At least one is needed "
                                "for a score to be proposed; send an empty list only when none exist")]

    @field_validator("reasons")
    @classmethod
    def _reasons(cls, v: list[str]) -> list[str]:
        return [check_text(x) for x in v]

    @field_validator("source_ids")
    @classmethod
    def _distinct(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("duplicate source")
        return v

    def log_args(self, secret):
        return {"contact_id": self.contact_id, "score": self.score,
                "confidence_e6": round(self.confidence * 1_000_000),
                "reasons": [secret(r) for r in self.reasons], "source_ids": list(self.source_ids)}


class TaskArgs(Args):
    contact_id: ContactId
    subject: Annotated[str, Field(min_length=1, max_length=100)]
    body: Annotated[str, Field(min_length=1, max_length=500, description="Plain text, no links")]
    due_date: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", description="YYYY-MM-DD, 1-60 days ahead")]

    @field_validator("subject")
    @classmethod
    def _subject(cls, v: str) -> str:
        return check_text(v)

    @field_validator("body")
    @classmethod
    def _body(cls, v: str) -> str:
        return check_text(v, newlines=True)

    @field_validator("due_date")
    @classmethod
    def _date(cls, v: str) -> str:
        dt.date.fromisoformat(v)
        return v

    def log_args(self, secret):
        return {"contact_id": self.contact_id, "subject": secret(self.subject), "body": secret(self.body),
                "due_date": self.due_date}


class NoArgs(Args):
    pass
