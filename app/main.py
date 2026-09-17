"""FastAPI application for the irradiation pulse budget service."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, status

from . import services
from .database import make_engine, make_session_factory
from .errors import register_exception_handlers
from .models import Base
from .schemas import (
    AuthorizationCreate,
    AuthorizationResponse,
    BatchCreate,
    BatchResponse,
)

DEFAULT_DATABASE_URL = "sqlite:///./data/app.db"


def create_app(database_url: str | None = None) -> FastAPI:
    url = database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        engine.dispose()

    app = FastAPI(title="Irradiation Pulse Budget API", version="1.0.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.session_factory = session_factory
    register_exception_handlers(app)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/batches", status_code=status.HTTP_201_CREATED, response_model=BatchResponse)
    def post_batch(payload: BatchCreate) -> BatchResponse:
        result = services.create_batch(session_factory, payload.batch_id, payload.budget)
        return BatchResponse(
            batch_id=result.batch_id, budget=result.budget, remaining=result.remaining
        )

    @app.get("/batches/{batch_id}", response_model=BatchResponse)
    def get_batch(batch_id: str) -> BatchResponse:
        result = services.get_batch(session_factory, batch_id)
        return BatchResponse(
            batch_id=result.batch_id, budget=result.budget, remaining=result.remaining
        )

    @app.post(
        "/authorizations",
        status_code=status.HTTP_201_CREATED,
        response_model=AuthorizationResponse,
    )
    def post_authorization(payload: AuthorizationCreate) -> AuthorizationResponse:
        result = services.authorize(
            session_factory, payload.batch_id, payload.request_key, payload.pulses
        )
        return _auth_response(result)

    @app.get("/authorizations/{request_key}", response_model=AuthorizationResponse)
    def get_authorization(request_key: str) -> AuthorizationResponse:
        return _auth_response(services.get_authorization(session_factory, request_key))

    return app


def _auth_response(result: services.AuthorizationResult) -> AuthorizationResponse:
    return AuthorizationResponse(
        authorization_id=result.authorization_id,
        request_key=result.request_key,
        batch_id=result.batch_id,
        pulses=result.pulses,
        remaining=result.remaining,
    )


app = create_app()
