"""Business logic: batch budgets and idempotent pulse authorizations.

Concurrency model
-----------------
SQLite serializes writers; every transaction starts with BEGIN IMMEDIATE (see
app.database) so a writer holds the database write lock from the start. The
deduction itself is a single atomic conditional UPDATE:

    UPDATE batches SET remaining = remaining - :pulses
    WHERE batch_id = :batch_id AND remaining >= :pulses

Two concurrent deductions therefore cannot both observe the same balance, the
successful pulse total can never exceed the initial budget, and the balance
can never go negative (also enforced by a CHECK constraint as defense in
depth). The ledger insert happens in the same transaction, so a failed insert
(e.g. duplicate request_key) rolls the deduction back with it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .errors import ApiError
from .models import Authorization, Batch

# Page-size bounds for the batch authorization review endpoint.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

_POSITIVE_INT_RE = re.compile(r"[0-9]+\Z")


@dataclass(frozen=True)
class BatchResult:
    batch_id: str
    budget: int
    remaining: int


@dataclass(frozen=True)
class AuthorizationResult:
    authorization_id: int
    request_key: str
    batch_id: str
    pulses: int
    remaining: int  # batch balance right after this authorization deducted


@dataclass(frozen=True)
class AuthorizationPage:
    """One stable review page.

    ``snapshot_max_id`` fixes the view for the whole pagination run: only
    authorizations whose id is <= it are counted or returned. New
    authorizations committed while the reviewer pages therefore neither mix
    into later pages nor push rows past the window (no omission).
    """

    batch_id: str
    items: tuple[AuthorizationResult, ...]
    used_pulses: int  # cumulative pulses of every auth with id <= snapshot_max_id
    snapshot_remaining: int  # balance implied by the snapshot view
    snapshot_budget: int  # batch budget at the time of the first request
    snapshot_max_id: int  # upper bound (inclusive) of the fixed view; 0 when empty
    next_position: int | None  # cursor for the following page, or None at the end


def _batch_snapshot(batch: Batch) -> BatchResult:
    return BatchResult(batch_id=batch.batch_id, budget=batch.budget, remaining=batch.remaining)


def _auth_snapshot(auth: Authorization) -> AuthorizationResult:
    return AuthorizationResult(
        authorization_id=auth.id,
        request_key=auth.request_key,
        batch_id=auth.batch_id,
        pulses=auth.pulses,
        remaining=auth.remaining_after,
    )


def create_batch(factory: sessionmaker, batch_id: str, budget: int) -> BatchResult:
    with factory() as session:
        if session.get(Batch, batch_id) is not None:
            raise ApiError(409, "BATCH_ALREADY_EXISTS", f"batch '{batch_id}' already exists")
        batch = Batch(batch_id=batch_id, budget=budget, remaining=budget)
        session.add(batch)
        try:
            session.flush()
            result = _batch_snapshot(batch)
            session.commit()
        except IntegrityError:
            # Lost a create race against a concurrent request with the same id.
            session.rollback()
            raise ApiError(409, "BATCH_ALREADY_EXISTS", f"batch '{batch_id}' already exists")
        return result


def get_batch(factory: sessionmaker, batch_id: str) -> BatchResult:
    with factory() as session:
        batch = session.get(Batch, batch_id)
        if batch is None:
            raise ApiError(404, "BATCH_NOT_FOUND", f"batch '{batch_id}' does not exist")
        return _batch_snapshot(batch)


def get_authorization(factory: sessionmaker, request_key: str) -> AuthorizationResult:
    result = _find_authorization(factory, request_key)
    if result is None:
        raise ApiError(404, "AUTHORIZATION_NOT_FOUND", f"request_key '{request_key}' not found")
    return result


def _pagination_error(message: str) -> ApiError:
    return ApiError(400, "PAGINATION_ERROR", message)


def _parse_page_size(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_PAGE_SIZE
    if not _POSITIVE_INT_RE.fullmatch(raw) or not (1 <= int(raw) <= MAX_PAGE_SIZE):
        raise _pagination_error(f"page_size must be an integer between 1 and {MAX_PAGE_SIZE}")
    return int(raw)


def _parse_cursor(raw: str | None, name: str, *, allow_zero: bool) -> int | None:
    if raw is None:
        return None
    if not _POSITIVE_INT_RE.fullmatch(raw):
        raise _pagination_error(f"{name} must be an integer")
    value = int(raw)
    if not allow_zero and value == 0:
        raise _pagination_error(f"{name} must be a positive integer")
    return value


def list_batch_authorizations(
    factory: sessionmaker,
    batch_id: str,
    page_size_raw: str | None = None,
    snapshot_max_id_raw: str | None = None,
    position_raw: str | None = None,
) -> AuthorizationPage:
    """List a batch's authorizations in id order over a snapshot-stable view.

    The first request (no cursor) pins ``snapshot_max_id`` to the batch's
    largest authorization id at that moment. Every page of the run — and
    every statistic on it — only considers authorizations with id <= that
    cap, so authorizations committed while the reviewer pages can neither
    mix into the view nor push older rows out of it. Follow-up requests
    carry the cap and the ``next_position`` cursor returned by the previous
    page.
    """
    page_size = _parse_page_size(page_size_raw)
    snapshot_max_id = _parse_cursor(snapshot_max_id_raw, "snapshot_max_id", allow_zero=True)
    position = _parse_cursor(position_raw, "position", allow_zero=False)

    # The cursor pair must be supplied together: a cap without a position or
    # a position without a cap is a contradictory snapshot.
    if (snapshot_max_id is None) != (position is None):
        raise _pagination_error(
            "snapshot_max_id and position must be provided together (first page provides neither)"
        )

    with factory() as session:
        batch = session.get(Batch, batch_id)
        if batch is None:
            raise ApiError(404, "BATCH_NOT_FOUND", f"batch '{batch_id}' does not exist")
        budget = batch.budget

        if snapshot_max_id is None:
            # First request: pin this review run to what currently exists.
            cap = session.scalar(
                select(func.coalesce(func.max(Authorization.id), 0)).where(
                    Authorization.batch_id == batch_id
                )
            )
        else:
            cap = snapshot_max_id
            if cap > 0:
                # The cap handed back by the client must be a real
                # authorization of this batch — otherwise a fabricated cap
                # larger than the current maximum would silently unpin the
                # view and later authorizations could leak into it.
                cap_owner = session.scalar(
                    select(Authorization.batch_id).where(Authorization.id == cap)
                )
                if cap_owner != batch_id:
                    raise _pagination_error(
                        f"snapshot_max_id {cap} does not belong to batch '{batch_id}'"
                    )
            if position >= cap:
                raise _pagination_error(
                    f"position {position} is not within snapshot_max_id {cap}"
                )
            # The cursor must be an authorization that belongs to this batch;
            # an id from another batch (or a nonexistent one) is rejected.
            owner = session.scalar(
                select(Authorization.batch_id).where(Authorization.id == position)
            )
            if owner != batch_id:
                raise _pagination_error(
                    f"position {position} does not belong to batch '{batch_id}'"
                )

        # All statistics are bounded by the pinned cap, including the sum —
        # so they stay identical across pages even as new authorizations land.
        used_pulses = session.scalar(
            select(func.coalesce(func.sum(Authorization.pulses), 0)).where(
                Authorization.batch_id == batch_id,
                Authorization.id <= cap,
            )
        )
        rows = session.scalars(
            select(Authorization)
            .where(Authorization.batch_id == batch_id)
            .where(Authorization.id > (position or 0))
            .where(Authorization.id <= cap)
            .order_by(Authorization.id)
            .limit(page_size + 1)
        ).all()

        has_more = len(rows) > page_size
        page_rows = rows[:page_size]
        next_position = page_rows[-1].id if has_more else None
        return AuthorizationPage(
            batch_id=batch_id,
            items=tuple(_auth_snapshot(row) for row in page_rows),
            used_pulses=used_pulses,
            snapshot_remaining=budget - used_pulses,
            snapshot_budget=budget,
            snapshot_max_id=cap,
            next_position=next_position,
        )


def authorize(
    factory: sessionmaker, batch_id: str, request_key: str, pulses: int
) -> AuthorizationResult:
    """Deduct `pulses` from `batch_id` once per `request_key`, idempotently.

    A retry with the same key and same business fields replays the stored
    response; the same key with different fields is a 409 conflict.
    """
    # Fast path: a committed ledger entry makes this call a replay or conflict.
    existing = _find_authorization(factory, request_key)
    if existing is not None:
        return _replay_or_conflict(existing, batch_id, pulses)

    failure: ApiError | None = None
    with factory() as session:
        try:
            # Atomic conditional deduction: only deducts while the balance
            # covers the request. This is the invariant guard — under any
            # interleaving, sum(pulses of successes) <= initial budget.
            rowcount = session.execute(
                update(Batch)
                .where(Batch.batch_id == batch_id)
                .where(Batch.remaining >= pulses)
                .values(remaining=Batch.remaining - pulses)
            ).rowcount
            if rowcount == 0:
                session.rollback()
                failure = _deduction_error(session, batch_id, pulses)
            else:
                remaining = session.scalar(
                    select(Batch.remaining).where(Batch.batch_id == batch_id)
                )
                auth = Authorization(
                    request_key=request_key,
                    batch_id=batch_id,
                    pulses=pulses,
                    remaining_after=remaining,
                )
                session.add(auth)
                session.flush()  # unique(request_key) is enforced here
                result = _auth_snapshot(auth)
                session.commit()
                return result
        except IntegrityError:
            # Another request with the same request_key committed first; the
            # deduction above is rolled back together with the failed insert.
            session.rollback()

    # A concurrent duplicate of this request_key may have committed while we
    # waited for the write lock: replay its stored response instead of
    # failing. This covers both the IntegrityError race and the case where
    # the balance was already consumed by our own winning duplicate.
    winner = _find_authorization(factory, request_key)
    if winner is not None:
        return _replay_or_conflict(winner, batch_id, pulses)
    if failure is not None:
        raise failure
    raise ApiError(500, "INTERNAL_ERROR", "idempotency ledger inconsistency")  # pragma: no cover


def _find_authorization(factory: sessionmaker, request_key: str) -> AuthorizationResult | None:
    with factory() as session:
        auth = session.scalar(select(Authorization).where(Authorization.request_key == request_key))
        return None if auth is None else _auth_snapshot(auth)


def _replay_or_conflict(
    existing: AuthorizationResult, batch_id: str, pulses: int
) -> AuthorizationResult:
    if existing.batch_id != batch_id or existing.pulses != pulses:
        raise ApiError(
            409,
            "REQUEST_KEY_CONFLICT",
            "request_key was already used with a different batch_id or pulses",
        )
    return existing


def _deduction_error(session: Session, batch_id: str, pulses: int) -> ApiError:
    batch = session.get(Batch, batch_id)
    if batch is None:
        return ApiError(404, "BATCH_NOT_FOUND", f"batch '{batch_id}' does not exist")
    return ApiError(
        409,
        "INSUFFICIENT_BUDGET",
        f"batch '{batch_id}' has {batch.remaining} pulses remaining, requested {pulses}",
    )
