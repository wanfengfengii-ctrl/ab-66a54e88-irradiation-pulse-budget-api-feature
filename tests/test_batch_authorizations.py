import pytest

LIST_PATH = "/batches/b1/authorizations"


def _make_batch(client, batch_id="b1", budget=1000):
    r = client.post("/batches", json={"batch_id": batch_id, "budget": budget})
    assert r.status_code == 201


def _authorize(client, key, pulses, batch_id="b1"):
    r = client.post(
        "/authorizations",
        json={"batch_id": batch_id, "request_key": key, "pulses": pulses},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _walk(client, page_size, path=LIST_PATH):
    """Follow next_position until the last page; return (pages, bodies)."""
    pages, bodies = [], []
    params = {"page_size": page_size}
    while True:
        r = client.get(path, params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        pages.append(body["items"])
        bodies.append(body)
        if body["next_position"] is None:
            assert body["has_more"] is False
            break
        assert body["has_more"] is True
        params = {
            "page_size": page_size,
            "position": body["next_position"],
            "snapshot_max_id": body["snapshot_max_id"],
        }
    return pages, bodies


def test_multi_page_walk_covers_every_authorization_in_id_order(client):
    _make_batch(client, budget=1000)
    pulses_seq = [10, 20, 5, 15, 25, 30, 7]
    created = [_authorize(client, f"k{i}", p) for i, p in enumerate(pulses_seq)]

    pages, bodies = _walk(client, page_size=3)

    assert len(pages) == 3
    assert [len(p) for p in pages] == [3, 3, 1]
    items = [item for page in pages for item in page]
    assert [item["authorization_id"] for item in items] == [
        c["authorization_id"] for c in created
    ]
    assert [item["pulses"] for item in items] == pulses_seq
    # items are ascending, dense and unique
    ids = [item["authorization_id"] for item in items]
    assert ids == sorted(ids) and len(ids) == len(set(ids))
    assert ids == list(range(ids[0], ids[0] + len(ids)))
    # per-authorization remaining snapshots match the deduction responses
    assert [item["remaining"] for item in items] == [c["remaining"] for c in created]

    # positions thread through the pages
    assert [b["position"] for b in bodies] == [0, 3, 6]
    assert [b["next_position"] for b in bodies] == [3, 6, None]
    # every page shares the snapshot pinned by the first request
    assert {b["snapshot_max_id"] for b in bodies} == {ids[-1]}
    assert {b["used_pulses"] for b in bodies} == {sum(pulses_seq)}
    assert {b["snapshot_remaining"] for b in bodies} == {1000 - sum(pulses_seq)}
    assert {b["snapshot_budget"] for b in bodies} == {1000}


def test_snapshot_pinned_view_ignores_authorizations_inserted_between_pages(client):
    _make_batch(client, budget=1000)
    for i in range(4):
        _authorize(client, f"k{i}", 10)

    first = client.get(LIST_PATH, params={"page_size": 2})
    assert first.status_code == 200
    p1 = first.json()
    pinned = p1["snapshot_max_id"]
    assert p1["position"] == 0 and p1["next_position"] == 2
    assert [it["authorization_id"] for it in p1["items"]] == [1, 2]

    # New authorizations arrive while the operator is still checking pages.
    late = [_authorize(client, f"new-{i}", 10) for i in range(3)]
    assert all(a["authorization_id"] > pinned for a in late)

    p2 = client.get(
        LIST_PATH,
        params={"page_size": 2, "position": p1["next_position"], "snapshot_max_id": pinned},
    ).json()
    assert [it["authorization_id"] for it in p2["items"]] == [3, 4]
    assert p2["next_position"] is None and p2["has_more"] is False
    # the view, its cap and every statistic stay frozen at the first request
    assert p2["snapshot_max_id"] == pinned
    assert p2["used_pulses"] == 40
    assert p2["snapshot_remaining"] == 960

    # restarting from page 1 with the same pinned snapshot is also stable
    replay = client.get(
        LIST_PATH, params={"page_size": 2, "position": 0, "snapshot_max_id": pinned}
    ).json()
    assert [it["authorization_id"] for it in replay["items"]] == [1, 2]
    assert replay["used_pulses"] == 40 and replay["snapshot_remaining"] == 960

    # a brand-new first request sees the current state, including the late ones
    fresh = client.get(LIST_PATH, params={"page_size": 10}).json()
    assert fresh["snapshot_max_id"] == max(a["authorization_id"] for a in late)
    assert len(fresh["items"]) == 7
    assert fresh["used_pulses"] == 70 and fresh["snapshot_remaining"] == 930


def test_empty_batch_returns_empty_snapshot(client):
    _make_batch(client, budget=42)
    r = client.get(LIST_PATH, params={"page_size": 10})
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "batch_id": "b1",
        "snapshot_max_id": None,
        "used_pulses": 0,
        "snapshot_remaining": 42,
        "snapshot_budget": 42,
        "page_size": 10,
        "position": 0,
        "next_position": None,
        "has_more": False,
        "items": [],
    }

    # authorizations granted after the empty view do not appear when replaying
    # it (there is no cap to echo), but do appear in a fresh first request
    _authorize(client, "k1", 7)
    again = client.get(LIST_PATH, params={"page_size": 10})
    assert again.json()["snapshot_max_id"] == 1
    assert [it["authorization_id"] for it in again.json()["items"]] == [1]
    assert again.json()["used_pulses"] == 7 and again.json()["snapshot_remaining"] == 35


def test_unknown_batch_still_returns_batch_not_found(client):
    r = client.get("/batches/ghost/authorizations", params={"page_size": 10})
    assert r.status_code == 404
    assert r.json()["code"] == "BATCH_NOT_FOUND"


@pytest.mark.parametrize("page_size", [0, -1, 201, 10_000])
def test_page_size_out_of_bounds_is_pagination_error(client, page_size):
    _make_batch(client)
    r = client.get(LIST_PATH, params={"page_size": page_size})
    assert r.status_code == 400
    assert r.json()["code"] == "PAGINATION_ERROR"


def test_position_without_snapshot_is_pagination_error(client):
    _make_batch(client)
    _authorize(client, "k1", 1)
    r = client.get(LIST_PATH, params={"page_size": 1, "position": 1})
    assert r.status_code == 400
    assert r.json()["code"] == "PAGINATION_ERROR"


def test_position_past_end_of_snapshot_is_pagination_error(client):
    _make_batch(client)
    _authorize(client, "k1", 1)
    pinned = client.get(LIST_PATH, params={"page_size": 1}).json()["snapshot_max_id"]
    r = client.get(
        LIST_PATH,
        params={"page_size": 1, "position": 99, "snapshot_max_id": pinned},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "PAGINATION_ERROR"


def test_snapshot_from_another_batch_is_pagination_error(client):
    _make_batch(client, batch_id="b1", budget=100)
    _make_batch(client, batch_id="b2", budget=100)
    _authorize(client, "k1", 1, batch_id="b1")
    other = _authorize(client, "k2", 1, batch_id="b2")
    r = client.get(
        "/batches/b1/authorizations",
        params={"page_size": 1, "position": 0, "snapshot_max_id": other["authorization_id"]},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "PAGINATION_ERROR"


def test_non_positive_snapshot_id_is_pagination_error(client):
    _make_batch(client)
    r = client.get(LIST_PATH, params={"page_size": 1, "snapshot_max_id": 0})
    assert r.status_code == 400
    assert r.json()["code"] == "PAGINATION_ERROR"


def test_pagination_errors_do_not_affect_deductions(client):
    _make_batch(client, budget=100)
    _authorize(client, "k1", 30)
    pinned = client.get(LIST_PATH, params={"page_size": 1}).json()["snapshot_max_id"]

    for params in (
        {"page_size": 0},
        {"page_size": 1, "position": 5},
        {"page_size": 1, "position": 1},
        {"page_size": 1, "position": 0, "snapshot_max_id": pinned + 1000},
    ):
        r = client.get(LIST_PATH, params=params)
        assert r.status_code == 400 and r.json()["code"] == "PAGINATION_ERROR"

    # balance and ledger untouched; deduction still works normally
    assert client.get("/batches/b1").json()["remaining"] == 70
    ok = _authorize(client, "k2", 70)
    assert ok["remaining"] == 0
    r = client.post(
        "/authorizations", json={"batch_id": "b1", "request_key": "k3", "pulses": 1}
    )
    assert r.status_code == 409 and r.json()["code"] == "INSUFFICIENT_BUDGET"


def test_default_page_size_when_param_omitted(client):
    _make_batch(client, budget=10_000)
    for i in range(3):
        _authorize(client, f"k{i}", 1)
    body = client.get(LIST_PATH).json()
    assert body["page_size"] == 50
    assert len(body["items"]) == 3 and body["has_more"] is False


def test_listing_does_not_block_or_duplicate_concurrent_deductions(app, client):
    # The listing runs on the read engine (deferred tx); a long-ish walk must
    # coexist with deductions, and the pinned view must stay stable.
    client.post("/batches", json={"batch_id": "b1", "budget": 100_000})
    for i in range(6):
        _authorize(client, f"k{i}", 10)

    first = client.get(LIST_PATH, params={"page_size": 2}).json()
    pinned = first["snapshot_max_id"]
    for i in range(10):
        _authorize(client, f"mid-{i}", 10)

    # continue the walk from the already-pinned first page
    pages, bodies = [first["items"]], [first]
    params = {"page_size": 2, "position": first["next_position"], "snapshot_max_id": pinned}
    while True:
        body = client.get(LIST_PATH, params=params).json()
        pages.append(body["items"])
        bodies.append(body)
        if body["next_position"] is None:
            break
        params["position"] = body["next_position"]

    ids = [it["authorization_id"] for page in pages for it in page]
    assert ids == list(range(1, 7))
    assert max(ids) == pinned
    assert {b["snapshot_max_id"] for b in bodies} == {pinned}
    assert {b["used_pulses"] for b in bodies} == {60}
    assert client.get("/batches/b1").json()["remaining"] == 100_000 - 160
