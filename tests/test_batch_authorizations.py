"""Batch authorization review: snapshot-stable keyset pagination."""
import pytest


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


def _fetch_all(client, batch_id, page_size):
    """Walk the whole review run from a first request; return (pages, body0)."""
    pages = []
    r = client.get(f"/batches/{batch_id}/authorizations?page_size={page_size}")
    assert r.status_code == 200, r.text
    body = r.json()
    pages.append(body)
    while body["next_position"] is not None:
        r = client.get(
            f"/batches/{batch_id}/authorizations"
            f"?page_size={page_size}"
            f"&snapshot_max_id={body['snapshot_max_id']}"
            f"&position={body['next_position']}"
        )
        assert r.status_code == 200, r.text
        body = r.json()
        pages.append(body)
    return pages, pages[0]


def test_first_page_shape_and_default_page_size(client):
    _make_batch(client)
    for i in range(25):
        _authorize(client, f"k{i}", 1)

    r = client.get("/batches/b1/authorizations")  # default page size
    assert r.status_code == 200
    body = r.json()
    assert body["batch_id"] == "b1"
    assert len(body["items"]) == 20
    assert [it["authorization_id"] for it in body["items"]] == list(range(1, 21))
    assert body["snapshot_max_id"] == 25
    assert body["next_position"] == 20
    # statistics cover the whole snapshot, not just the page
    assert body["used_pulses"] == 25
    assert body["snapshot_budget"] == 1000
    assert body["snapshot_remaining"] == 975

    r2 = client.get(
        f"/batches/b1/authorizations?snapshot_max_id=25&position={body['next_position']}"
    )
    assert r2.status_code == 200
    body2 = r2.json()
    assert len(body2["items"]) == 5
    assert [it["authorization_id"] for it in body2["items"]] == list(range(21, 26))
    assert body2["next_position"] is None
    # statistics stay identical on every page
    for key in ("used_pulses", "snapshot_remaining", "snapshot_budget", "snapshot_max_id"):
        assert body2[key] == body[key]


def test_multi_page_review_walks_every_authorization_in_id_order(client):
    _make_batch(client, budget=100)
    pulses = [10, 20, 5, 15, 7]
    for i, p in enumerate(pulses):
        _authorize(client, f"k{i}", p)

    pages, first = _fetch_all(client, "b1", 2)
    assert len(pages) == 3
    assert [len(p["items"]) for p in pages] == [2, 2, 1]

    items = [it for page in pages for it in page["items"]]
    ids = [it["authorization_id"] for it in items]
    assert ids == sorted(ids) and len(ids) == 5
    assert [it["pulses"] for it in items] == pulses
    # item snapshots are the post-deduction balances, strictly decreasing
    assert [it["remaining"] for it in items] == [90, 70, 65, 50, 43]
    assert all(page["snapshot_max_id"] == first["snapshot_max_id"] for page in pages)
    assert all(page["used_pulses"] == 57 for page in pages)
    assert all(page["snapshot_remaining"] == 43 for page in pages)
    assert pages[-1]["next_position"] is None


def test_new_authorizations_between_pages_never_mix_in_and_nothing_is_omitted(client):
    _make_batch(client, budget=1000)
    for i in range(4):
        _authorize(client, f"k{i}", 10)

    first = client.get("/batches/b1/authorizations?page_size=2").json()
    cap = first["snapshot_max_id"]
    assert cap == 4
    assert [it["authorization_id"] for it in first["items"]] == [1, 2]

    # New authorization lands while the reviewer reads the next page.
    _authorize(client, "k-mid", 30)
    # An authorization in another batch must not disturb the cursor either.
    _make_batch(client, batch_id="other", budget=100)
    _authorize(client, "k-other", 40, batch_id="other")

    second = client.get(
        f"/batches/b1/authorizations?page_size=2&snapshot_max_id={cap}&position={first['next_position']}"
    ).json()
    assert [it["authorization_id"] for it in second["items"]] == [3, 4]
    assert second["next_position"] is None
    # The run still ends at the old cap: the new rows are absent everywhere,
    # and the statistics never counted them.
    assert second["snapshot_max_id"] == 4
    assert second["used_pulses"] == 40
    assert second["snapshot_remaining"] == 960

    # A fresh review run started afterwards sees the new authorization.
    fresh = client.get("/batches/b1/authorizations?page_size=10").json()
    assert [it["authorization_id"] for it in fresh["items"]] == [1, 2, 3, 4, 5]
    assert fresh["used_pulses"] == 70
    assert fresh["snapshot_remaining"] == 930


def test_empty_batch_returns_empty_snapshot(client):
    _make_batch(client, batch_id="empty", budget=8)
    r = client.get("/batches/empty/authorizations")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "batch_id": "empty",
        "items": [],
        "used_pulses": 0,
        "snapshot_remaining": 8,
        "snapshot_budget": 8,
        "snapshot_max_id": 0,
        "next_position": None,
    }


def test_unknown_batch_keeps_existing_machine_code(client):
    r = client.get("/batches/ghost/authorizations")
    assert r.status_code == 404
    assert r.json()["code"] == "BATCH_NOT_FOUND"


@pytest.mark.parametrize("bad_page_size", ["0", "-1", "101", "abc", "1.5", ""])
def test_page_size_out_of_bounds_is_pagination_error(client, bad_page_size):
    _make_batch(client)
    r = client.get(f"/batches/b1/authorizations?page_size={bad_page_size}")
    assert r.status_code == 400
    assert r.json()["code"] == "PAGINATION_ERROR"


def test_page_size_boundary_values(client):
    _make_batch(client)
    _authorize(client, "k1", 1)
    assert client.get("/batches/b1/authorizations?page_size=1").status_code == 200
    assert client.get("/batches/b1/authorizations?page_size=100").status_code == 200


@pytest.mark.parametrize("bad_position", ["0", "-1", "abc", "1.5", ""])
def test_malformed_position_is_pagination_error(client, bad_position):
    _make_batch(client)
    _authorize(client, "k1", 1)
    r = client.get(
        f"/batches/b1/authorizations?snapshot_max_id=1&position={bad_position}"
    )
    assert r.status_code == 400
    assert r.json()["code"] == "PAGINATION_ERROR"


def test_position_from_another_batch_is_pagination_error(client):
    # The foreign authorization is created first, so its id is *smaller*
    # than b1's cap — the position-ownership check, not the position>=cap
    # check, must reject it.
    _make_batch(client, batch_id="other")
    foreign = _authorize(client, "k-other", 1, batch_id="other")
    _make_batch(client, batch_id="b1")
    own = _authorize(client, "k1", 1, batch_id="b1")
    r = client.get(
        f"/batches/b1/authorizations"
        f"?snapshot_max_id={own['authorization_id']}"
        f"&position={foreign['authorization_id']}"
    )
    assert r.status_code == 400
    assert r.json()["code"] == "PAGINATION_ERROR"


def test_nonexistent_cursors_are_pagination_errors(client):
    _make_batch(client)
    _authorize(client, "k1", 1)
    r = client.get("/batches/b1/authorizations?snapshot_max_id=999&position=1")
    assert r.status_code == 400
    assert r.json()["code"] == "PAGINATION_ERROR"
    r = client.get("/batches/b1/authorizations?snapshot_max_id=1&position=999")
    assert r.status_code == 400
    assert r.json()["code"] == "PAGINATION_ERROR"


def test_position_outside_cap_is_pagination_error(client):
    _make_batch(client)
    for i in range(3):
        _authorize(client, f"k{i}", 1)
    # position equals cap (already consumed / beyond window)
    r = client.get("/batches/b1/authorizations?snapshot_max_id=2&position=2")
    assert r.status_code == 400
    assert r.json()["code"] == "PAGINATION_ERROR"
    # position greater than cap
    r = client.get("/batches/b1/authorizations?snapshot_max_id=1&position=2")
    assert r.status_code == 400
    assert r.json()["code"] == "PAGINATION_ERROR"


def test_cursor_pair_must_be_supplied_together(client):
    _make_batch(client)
    _authorize(client, "k1", 1)
    only_position = client.get("/batches/b1/authorizations?position=1")
    assert only_position.status_code == 400
    assert only_position.json()["code"] == "PAGINATION_ERROR"
    only_cap = client.get("/batches/b1/authorizations?snapshot_max_id=1")
    assert only_cap.status_code == 400
    assert only_cap.json()["code"] == "PAGINATION_ERROR"


def test_cap_from_another_batch_is_pagination_error(client):
    _make_batch(client, batch_id="b1")
    _make_batch(client, batch_id="b2")
    _authorize(client, "k1", 1, batch_id="b1")
    other = _authorize(client, "k2", 1, batch_id="b2")
    r = client.get(
        f"/batches/b1/authorizations?snapshot_max_id={other['authorization_id']}&position=1"
    )
    assert r.status_code == 400
    assert r.json()["code"] == "PAGINATION_ERROR"


def test_pagination_errors_do_not_affect_deductions(client):
    _make_batch(client, budget=100)
    _authorize(client, "k1", 10)
    for url in [
        "/batches/b1/authorizations?page_size=0",
        "/batches/b1/authorizations?page_size=999",
        "/batches/b1/authorizations?position=1",
        "/batches/b1/authorizations?snapshot_max_id=1",
        "/batches/b1/authorizations?snapshot_max_id=1&position=1",
        "/batches/b1/authorizations?snapshot_max_id=999&position=1",
    ]:
        assert client.get(url).status_code == 400
    # read-only review (even malformed) never moves the balance
    assert client.get("/batches/b1").json()["remaining"] == 90
    ok = client.post(
        "/authorizations", json={"batch_id": "b1", "request_key": "k2", "pulses": 20}
    )
    assert ok.status_code == 201
    assert ok.json()["remaining"] == 70


def test_legacy_endpoints_remain_compatible(client):
    _make_batch(client, budget=50)
    auth = client.post(
        "/authorizations", json={"batch_id": "b1", "request_key": "rk", "pulses": 5}
    )
    assert auth.status_code == 201
    assert auth.json() == {
        "authorization_id": auth.json()["authorization_id"],
        "request_key": "rk",
        "batch_id": "b1",
        "pulses": 5,
        "remaining": 45,
    }
    # replay still returns the original 201 body
    replay = client.post(
        "/authorizations", json={"batch_id": "b1", "request_key": "rk", "pulses": 5}
    )
    assert replay.status_code == 201 and replay.json() == auth.json()
    assert client.get("/batches/b1").json() == {
        "batch_id": "b1",
        "budget": 50,
        "remaining": 45,
    }
    assert client.get("/authorizations/rk").json() == auth.json()
