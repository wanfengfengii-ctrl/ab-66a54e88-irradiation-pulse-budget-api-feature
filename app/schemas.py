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
