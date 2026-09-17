"""Request and response schemas for the API."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# SQLite INTEGER is a signed 64-bit value; bounding inputs keeps arithmetic safe.
MAX_INT64 = 9_223_372_036_854_775_807

# Bounds for the authorization-detail listing page size.
MIN_PAGE_SIZE = 1
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


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


class BatchAuthorizationItem(BaseModel):
    """One authorization row in a batch's audit listing."""

    authorization_id: int
    request_key: str
    pulses: int
    remaining: int  # batch balance snapshot right after this authorization


class BatchAuthorizationsPage(BaseModel):
    """One fixed-snapshot page of a batch's authorizations.

    `snapshot_budget`/`snapshot_remaining` and `used_pulses` are computed only
    over authorizations with id <= `snapshot_max_id`, so the view is stable
    while paging even if new authorizations are granted concurrently. The next
    request must echo both `snapshot_max_id` and `next_position`.
    """

    batch_id: str
    snapshot_max_id: int | None
    used_pulses: int
    snapshot_remaining: int
    snapshot_budget: int
    page_size: int
    position: int
    next_position: int | None
    has_more: bool
    items: list[BatchAuthorizationItem]
