"""Typed output schemas the quarantined reader is allowed to return.

This is the whole trick against a poisoned reader: the planner never sees the reader's text,
only a parsed instance of one of these models. `extra="forbid"` drops nothing silently and rejects
anything that is not a declared field; strings are length-capped so they cannot smuggle a prompt.
Free-text fields exist, but policy treats them as display-only data.
"""
from __future__ import annotations

from typing import Dict, List, Literal, Type

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=400)


class YesNo(_Strict):
    """A single boolean answer to the planner's question."""
    answer: bool
    confidence: float = Field(ge=0, le=1, default=0.5)


KEEP_CATEGORIES = {"personal", "work", "receipt"}


class EmailTriage(_Strict):
    """Classify one email for cleanup decisions."""
    should_delete: bool
    category: Literal["marketing", "newsletter", "notification", "personal", "work", "receipt", "other"]
    reason: str = Field(max_length=200, default="")

    @model_validator(mode="after")
    def _never_delete_personal(self) -> "EmailTriage":
        # A small reader sometimes says category=personal and should_delete=true. Resolve the
        # contradiction in code, toward keeping the email.
        if self.category in KEEP_CATEGORIES:
            self.should_delete = False
        return self


class ShortSummary(_Strict):
    """A short, display-only summary of untrusted text."""
    summary: str = Field(max_length=400)


class Answer(_Strict):
    """A short, plain-language answer to the owner's request, from the data given. Display only."""
    answer: str = Field(max_length=600)


class ExtractedDate(_Strict):
    """An ISO date (YYYY-MM-DD) found in the text, or null if none."""
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class ExtractedNumber(_Strict):
    """A single number found in the text (e.g. an amount), or null if none."""
    value: float | None = None
    unit: str | None = Field(default=None, max_length=20)


class NamedEntities(_Strict):
    """Up to 10 people/organisations mentioned in the text."""
    names: List[str] = Field(max_length=10, default_factory=list)


SCHEMAS: Dict[str, Type[_Strict]] = {
    "YesNo": YesNo,
    "EmailTriage": EmailTriage,
    "ShortSummary": ShortSummary,
    "Answer": Answer,
    "ExtractedDate": ExtractedDate,
    "ExtractedNumber": ExtractedNumber,
    "NamedEntities": NamedEntities,
}


def catalog() -> str:
    lines = []
    for name, model in SCHEMAS.items():
        fields = ", ".join(f"{k}: {v.annotation}" for k, v in model.model_fields.items())
        lines.append(f"- {name}: {model.__doc__.strip() if model.__doc__ else ''} Fields: {fields}")
    return "\n".join(lines)
