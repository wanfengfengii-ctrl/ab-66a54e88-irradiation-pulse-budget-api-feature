"""FastAPI application for the irradiation pulse budget service."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, status

from . import services
from .database import make_engines, make_session_factory
from .errors import register_exception_handlers
from .models import Base
from .schemas import (
    AuthorizationCreate,
    AuthorizationResponse,
    BatchAuthorizationItem,
    BatchAuthorizationsPage,
    BatchCreate,
    BatchResponse,
    DEFAULT_PAGE_SIZE,
)

DEFAULT_DATABASE_URL = "sqlite:///./data/app.db"


def create_app(database_url: str | None = None) -> FastAPI:
    url = database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    write_engine, read_engine = make_engines(url)
    Base.metadata.create_all(write_engine)
    session_factory = make_session_factory(write_engine)
    read_session_factory = make_session_factory(read_engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        write_engine.dispose()
        read_engine.dispose()

    app = FastAPI(title="Irradiation Pulse Budget API", version="1.0.0", lifespan=lifespan)
    app.state.engine = write_engine
    app.state.read_engine = read_engine
    app.state.session_factory = session_factory
    app.state.read_session_factory = read_session_factory
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

    @app.get("/batches/{batch_id}/authorizations", response_model=BatchAuthorizationsPage)
    def list_batch_authorizations(
        batch_id: str,
        page_size: int = DEFAULT_PAGE_SIZE,
        position: int = 0,
        snapshot_max_id: int | None = None,
    ) -> BatchAuthorizationsPage:
        result = services.list_batch_authorizations(
            read_session_factory,
            batch_id,
            page_size=page_size,
            position=position,
            snapshot_max_id=snapshot_max_id,
        )
        return BatchAuthorizationsPage(
            batch_id=result.batch_id,
            snapshot_max_id=result.snapshot_max_id,
            used_pulses=result.used_pulses,
            snapshot_remaining=result.snapshot_remaining,
            snapshot_budget=result.snapshot_budget,
            page_size=result.page_size,
            position=result.position,
            next_position=result.next_position,
            has_more=result.has_more,
            items=[
                BatchAuthorizationItem(
                    authorization_id=item.authorization_id,
                    request_key=item.request_key,
                    pulses=item.pulses,
                    remaining=item.remaining,
                )
                for item in result.items
            ],
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
