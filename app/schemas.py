"""Request and response schemas for the API."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# SQLite INTEGER is a signed 64-bit value; bounding inputs keeps arithmetic safe.
MAX_INT64 = 9_223_372_036_854_775_807


class BatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1, max_length=128)
    budget: int = Field(gt=0, le=MAX_INT64)


class AuthorizationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1, max_length=128)
    request_key: str = Field(min_length=1, max_length=256)
    pulses: int = Field(gt=0, le=MAX_INT64)


class BatchResponse(BaseModel):
    batch_id: str
    budget: int
    remaining: int


class AuthorizationResponse(BaseModel):
    authorization_id: int
    request_key: str
    batch_id: str
    pulses: int
    remaining: int


class AuthorizationListItem(BaseModel):
    authorization_id: int
    request_key: str
    batch_id: str
    pulses: int
    remaining: int


class AuthorizationPageResponse(BaseModel):
    batch_id: str
    items: list[AuthorizationListItem]
    # Cumulative pulses used within the pinned snapshot view.
    used_pulses: int
    # Balance implied by the snapshot (budget - used_pulses).
    snapshot_remaining: int
    snapshot_budget: int
    # Largest authorization id included in this review run; fixed on the
    # first request and echoed on every follow-up.
    snapshot_max_id: int
    # Cursor for the next page (last id of the current page), or null at end.
    next_position: int | None = None
