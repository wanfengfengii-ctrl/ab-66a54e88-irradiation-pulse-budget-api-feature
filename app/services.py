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

from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .errors import ApiError
from .models import Authorization, Batch
from .schemas import MAX_PAGE_SIZE, MIN_PAGE_SIZE


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
class BatchAuthorizationItem:
    authorization_id: int
    request_key: str
    pulses: int
    remaining: int


@dataclass(frozen=True)
class BatchAuthorizationsResult:
    batch_id: str
    snapshot_max_id: int | None
    used_pulses: int
    snapshot_remaining: int
    snapshot_budget: int
    page_size: int
    position: int
    next_position: int | None
    has_more: bool
    items: tuple[BatchAuthorizationItem, ...]


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


def _pagination_error(message: str) -> ApiError:
    return ApiError(400, "PAGINATION_ERROR", message)


def list_batch_authorizations(
    factory: sessionmaker,
    batch_id: str,
    page_size: int,
    position: int,
    snapshot_max_id: int | None,
) -> BatchAuthorizationsResult:
    """Return one ascending-id page of the batch's authorizations.

    The first call (position 0, no snapshot_max_id) fixes this round's view at
    the batch's current maximum authorization id; later calls echo that id and
    the previous next_position. Every statistic and row is computed only over
    authorizations with id <= snapshot_max_id, so authorizations granted
    mid-paging neither enter the listing nor push anything off later pages.

    Runs on the read-only factory (deferred transactions): it never takes the
    write lock and never blocks pulse deductions.
    """
    if page_size < MIN_PAGE_SIZE or page_size > MAX_PAGE_SIZE:
        raise _pagination_error(
            f"page_size must be between {MIN_PAGE_SIZE} and {MAX_PAGE_SIZE}"
        )
    if position < 0:
        raise _pagination_error("position must be >= 0")
    if position > 0 and snapshot_max_id is None:
        raise _pagination_error("position requires snapshot_max_id from a previous page")
    if snapshot_max_id is not None and snapshot_max_id <= 0:
        raise _pagination_error("snapshot_max_id must be a positive authorization id")

    with factory() as session:
        batch = session.get(Batch, batch_id)
        if batch is None:
            raise ApiError(404, "BATCH_NOT_FOUND", f"batch '{batch_id}' does not exist")
        budget = batch.budget

        if snapshot_max_id is None:
            # First call: pin the view at the largest authorization id the
            # batch has committed at this instant (None for an empty batch).
            max_id = session.scalar(
                select(func.max(Authorization.id)).where(Authorization.batch_id == batch_id)
            )
        else:
            # Continuation: the pinned id must be an authorization of *this*
            # batch, i.e. a snapshot this endpoint could have handed out for it.
            owner = session.scalar(
                select(Authorization.id).where(
                    Authorization.id == snapshot_max_id,
                    Authorization.batch_id == batch_id,
                )
            )
            if owner is None:
                raise _pagination_error("snapshot_max_id does not belong to this batch")
            max_id = snapshot_max_id

        if max_id is None:
            # First call on a batch with no authorizations yet: empty view.
            total, used = 0, 0
            rows = []
        else:
            view_filters = (
                Authorization.batch_id == batch_id,
                Authorization.id <= max_id,
            )
            # One read transaction: SQLite snapshot isolation keeps the count,
            # totals and page rows mutually consistent.
            total = session.scalar(
                select(func.count()).select_from(Authorization).where(*view_filters)
            )
            if position > total:
                raise _pagination_error("position is past the end of this snapshot")
            used = session.scalar(
                select(func.coalesce(func.sum(Authorization.pulses), 0)).where(*view_filters)
            )
            rows = list(
                session.scalars(
                    select(Authorization)
                    .where(*view_filters)
                    .order_by(Authorization.id.asc())
                    .limit(page_size + 1)
                    .offset(position)
                )
            )

        has_more = len(rows) > page_size
        rows = rows[:page_size]
        items = tuple(
            BatchAuthorizationItem(
                authorization_id=row.id,
                request_key=row.request_key,
                pulses=row.pulses,
                remaining=row.remaining_after,
            )
            for row in rows
        )

    return BatchAuthorizationsResult(
        batch_id=batch_id,
        snapshot_max_id=max_id,
        used_pulses=int(used),
        snapshot_remaining=budget - int(used),
        snapshot_budget=budget,
        page_size=page_size,
        position=position,
        next_position=position + len(items) if has_more else None,
        has_more=has_more,
        items=items,
    )


def get_authorization(factory: sessionmaker, request_key: str) -> AuthorizationResult:
    result = _find_authorization(factory, request_key)
    if result is None:
        raise ApiError(404, "AUTHORIZATION_NOT_FOUND", f"request_key '{request_key}' not found")
    return result


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
