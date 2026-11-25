"""F1. Policy: what is allowed. Anything absent is denied; YAML can only tighten."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .common import StartupError

CONTACT_FIELDS = {"lead_score", "lead_score_reason"}
TASK_FIELDS = {"hs_task_subject", "hs_task_body", "hs_timestamp", "hs_task_status", "hubspot_owner_id"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, frozen=True)


class _Tool(_Strict):
    allowed: bool


class SearchPolicy(_Tool):
    approval: Literal[False]
    per_day: Annotated[int, Field(ge=0, le=200)]
    max_days: Annotated[int, Field(ge=1, le=30)]


class CompanyPolicy(_Tool):
    approval: Literal[False]
    per_day: Annotated[int, Field(ge=0, le=200)]


class ResearchToolPolicy(_Tool):
    approval: Literal[False]
    per_day: Annotated[int, Field(ge=0, le=100)]


class ScorePolicy(_Tool):
    approval: Literal[True]
    per_day: Annotated[int, Field(ge=0, le=60)]


class TaskPolicy(_Tool):
    approval: Literal[True]
    per_day: Annotated[int, Field(ge=0, le=60)]
    max_due_days: Annotated[int, Field(ge=1, le=60)]


class SlackPolicy(_Tool):
    approval: Literal[False]
    per_day: Annotated[int, Field(ge=0, le=1)]


class Tools(_Strict):
    search_new_leads: SearchPolicy | None = None
    get_company: CompanyPolicy | None = None
    research_company: ResearchToolPolicy | None = None
    update_lead_score: ScorePolicy | None = None
    create_followup_task: TaskPolicy | None = None
    post_slack_summary: SlackPolicy | None = None


class Tiers(_Strict):
    A: Annotated[int, Field(ge=1, le=100)]
    B: Annotated[int, Field(ge=1, le=100)]

    @model_validator(mode="after")
    def _ordered(self) -> "Tiers":
        if self.A <= self.B:
            raise ValueError("tier A must be above tier B")
        return self


class ResearchLimits(_Strict):
    jobs_per_day: Annotated[int, Field(ge=0, le=30)]
    dollars_per_day: Annotated[float, Field(ge=0, le=3)]


class WriteFields(_Strict):
    contact: list[str]
    task: list[str]

    @model_validator(mode="after")
    def _subset(self) -> "WriteFields":
        if not set(self.contact) <= CONTACT_FIELDS or not set(self.task) <= TASK_FIELDS:
            raise ValueError("write_fields contains a field the code does not permit")
        return self


class Policy(_Strict):
    version: Literal[1]
    timezone: str
    writes_enabled: bool
    writes_per_day: Annotated[int, Field(ge=0, le=20)]
    min_confidence: Annotated[float, Field(ge=0.5, le=1)]
    tiers: Tiers
    research: ResearchLimits
    write_fields: WriteFields
    tools: Tools

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception:
            raise ValueError("unknown timezone") from None
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def tool(self, name: str):
        """The tool's rules, or None when it is not allowed."""
        if name not in Tools.model_fields:
            return None
        rules = getattr(self.tools, name)
        return rules if rules is not None and rules.allowed else None

    def allowed_tools(self) -> list[str]:
        return [n for n in Tools.model_fields if self.tool(n) is not None]

    def tier(self, score: int) -> str:
        return "A" if score >= self.tiers.A else "B" if score >= self.tiers.B else "C"


class _Loader(yaml.SafeLoader):
    pass


def _no_duplicates(loader: _Loader, node: yaml.MappingNode):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def load_policy(path: Path) -> tuple[Policy, str]:
    """Return the policy and the SHA-256 of its bytes."""
    try:
        raw = path.read_bytes()
    except OSError:
        raise StartupError(f"cannot read {path.name}") from None
    try:
        data = yaml.load(raw, Loader=_Loader)
        policy = Policy.model_validate(data)
    except Exception as e:  # PyYAML raises many types on odd input; all of them mean the same thing here
        detail = str(e) if isinstance(e, (yaml.YAMLError, ValidationError)) else type(e).__name__
        raise StartupError(f"invalid {path.name}: {detail}") from None
    return policy, hashlib.sha256(raw).hexdigest()
