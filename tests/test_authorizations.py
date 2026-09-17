def _make_batch(client, batch_id="b1", budget=100):
    r = client.post("/batches", json={"batch_id": batch_id, "budget": budget})
    assert r.status_code == 201


def test_authorize_success_returns_201_id_and_balance(client):
    _make_batch(client)
    r = client.post("/authorizations", json={"batch_id": "b1", "request_key": "k1", "pulses": 30})
    assert r.status_code == 201
    body = r.json()
    assert isinstance(body["authorization_id"], int) and body["authorization_id"] >= 1
    assert body["request_key"] == "k1"
    assert body["batch_id"] == "b1"
    assert body["pulses"] == 30
    assert body["remaining"] == 70
    assert client.get("/batches/b1").json()["remaining"] == 70


def test_retry_same_key_replays_identical_response_without_double_deduct(client):
    _make_batch(client)
    payload = {"batch_id": "b1", "request_key": "k1", "pulses": 30}
    first = client.post("/authorizations", json=payload)
    assert first.status_code == 201
    for _ in range(3):
        again = client.post("/authorizations", json=payload)
        assert again.status_code == 201
        assert again.json() == first.json()
    # deducted exactly once despite four identical requests
    assert client.get("/batches/b1").json()["remaining"] == 70


def test_same_key_different_pulses_conflict(client):
    _make_batch(client)
    client.post("/authorizations", json={"batch_id": "b1", "request_key": "k1", "pulses": 30})
    r = client.post("/authorizations", json={"batch_id": "b1", "request_key": "k1", "pulses": 31})
    assert r.status_code == 409
    assert r.json()["code"] == "REQUEST_KEY_CONFLICT"
    assert client.get("/batches/b1").json()["remaining"] == 70


def test_same_key_different_batch_conflict(client):
    _make_batch(client)
    _make_batch(client, batch_id="b2", budget=50)
    client.post("/authorizations", json={"batch_id": "b1", "request_key": "k1", "pulses": 10})
    r = client.post("/authorizations", json={"batch_id": "b2", "request_key": "k1", "pulses": 10})
    assert r.status_code == 409
    assert r.json()["code"] == "REQUEST_KEY_CONFLICT"
    assert client.get("/batches/b2").json()["remaining"] == 50


def test_insufficient_budget_creates_no_ledger_record(client):
    _make_batch(client, budget=10)
    r = client.post("/authorizations", json={"batch_id": "b1", "request_key": "k1", "pulses": 11})
    assert r.status_code == 409
    assert r.json()["code"] == "INSUFFICIENT_BUDGET"
    # no authorization and no ledger record were created
    assert client.get("/authorizations/k1").status_code == 404
    assert client.get("/batches/b1").json()["remaining"] == 10
    # the failed attempt did not bind the key: a smaller amount with the same
    # key is treated as a brand-new request and succeeds
    r = client.post("/authorizations", json={"batch_id": "b1", "request_key": "k1", "pulses": 10})
    assert r.status_code == 201
    assert r.json()["remaining"] == 0


def test_exact_budget_then_exhausted(client):
    _make_batch(client, budget=10)
    r = client.post("/authorizations", json={"batch_id": "b1", "request_key": "k1", "pulses": 10})
    assert r.status_code == 201
    assert r.json()["remaining"] == 0
    r = client.post("/authorizations", json={"batch_id": "b1", "request_key": "k2", "pulses": 1})
    assert r.status_code == 409
    assert r.json()["code"] == "INSUFFICIENT_BUDGET"
    assert client.get("/batches/b1").json()["remaining"] == 0


def test_unknown_batch_404(client):
    r = client.post("/authorizations", json={"batch_id": "ghost", "request_key": "k1", "pulses": 1})
    assert r.status_code == 404
    assert r.json()["code"] == "BATCH_NOT_FOUND"


def test_non_positive_pulses_rejected(client):
    _make_batch(client)
    for pulses in (0, -5):
        r = client.post(
            "/authorizations",
            json={"batch_id": "b1", "request_key": f"k{pulses}", "pulses": pulses},
        )
        assert r.status_code == 422
        assert r.json()["code"] == "VALIDATION_ERROR"


def test_sequential_deductions_accumulate_and_ids_increase(client):
    _make_batch(client, budget=100)
    bodies = []
    for i, pulses in enumerate([10, 20, 5]):
        r = client.post(
            "/authorizations",
            json={"batch_id": "b1", "request_key": f"k{i}", "pulses": pulses},
        )
        assert r.status_code == 201
        bodies.append(r.json())
    assert [b["remaining"] for b in bodies] == [90, 70, 65]
    ids = [b["authorization_id"] for b in bodies]
    assert len(set(ids)) == 3 and ids == sorted(ids)
    assert client.get("/batches/b1").json()["remaining"] == 65


def test_get_authorization_from_ledger(client):
    _make_batch(client)
    created = client.post(
        "/authorizations", json={"batch_id": "b1", "request_key": "k1", "pulses": 5}
    ).json()
    r = client.get("/authorizations/k1")
    assert r.status_code == 200
    assert r.json() == created
    assert client.get("/authorizations/absent").status_code == 404
    assert client.get("/authorizations/absent").json()["code"] == "AUTHORIZATION_NOT_FOUND"
