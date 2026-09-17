from fastapi.testclient import TestClient

from app.main import create_app


def _restart(db_url, app):
    """Simulate a process restart: drop every connection, rebuild the app."""
    app.state.engine.dispose()
    return create_app(db_url)


def test_state_and_replay_survive_restart(db_url):
    app = create_app(db_url)
    client = TestClient(app)
    client.post("/batches", json={"batch_id": "b1", "budget": 50})
    r1 = client.post("/authorizations", json={"batch_id": "b1", "request_key": "k1", "pulses": 20})
    assert r1.status_code == 201

    app = _restart(db_url, app)
    client = TestClient(app)

    # retry of a pre-restart request replays the exact original response
    replay = client.post("/authorizations", json={"batch_id": "b1", "request_key": "k1", "pulses": 20})
    assert replay.status_code == 201
    assert replay.json() == r1.json()

    # balance persisted, not reset to the initial budget
    assert client.get("/batches/b1").json() == {"batch_id": "b1", "budget": 50, "remaining": 30}

    # new deductions continue from the persisted balance
    r2 = client.post("/authorizations", json={"batch_id": "b1", "request_key": "k2", "pulses": 30})
    assert r2.status_code == 201
    assert r2.json()["remaining"] == 0
    assert r2.json()["authorization_id"] > r1.json()["authorization_id"]

    # budget is still exhausted after the restart: no silent top-up
    r3 = client.post("/authorizations", json={"batch_id": "b1", "request_key": "k3", "pulses": 1})
    assert r3.status_code == 409
    assert r3.json()["code"] == "INSUFFICIENT_BUDGET"
    assert client.get("/batches/b1").json()["remaining"] == 0

    # batch uniqueness still enforced after restart
    dup = client.post("/batches", json={"batch_id": "b1", "budget": 999})
    assert dup.status_code == 409
    assert dup.json()["code"] == "BATCH_ALREADY_EXISTS"


def test_key_conflict_survives_restart(db_url):
    app = create_app(db_url)
    client = TestClient(app)
    client.post("/batches", json={"batch_id": "b1", "budget": 10})
    client.post("/authorizations", json={"batch_id": "b1", "request_key": "k1", "pulses": 5})

    app = _restart(db_url, app)
    client = TestClient(app)

    r = client.post("/authorizations", json={"batch_id": "b1", "request_key": "k1", "pulses": 6})
    assert r.status_code == 409
    assert r.json()["code"] == "REQUEST_KEY_CONFLICT"


def test_double_restart_keeps_ledger(db_url):
    app = create_app(db_url)
    client = TestClient(app)
    client.post("/batches", json={"batch_id": "b1", "budget": 100})
    first = client.post("/authorizations", json={"batch_id": "b1", "request_key": "k1", "pulses": 1})

    for _ in range(2):
        app = _restart(db_url, app)
        client = TestClient(app)
        replay = client.post(
            "/authorizations", json={"batch_id": "b1", "request_key": "k1", "pulses": 1}
        )
        assert replay.status_code == 201
        assert replay.json() == first.json()
        assert client.get("/batches/b1").json()["remaining"] == 99
