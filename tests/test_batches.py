import pytest


def test_create_batch_returns_201(client):
    r = client.post("/batches", json={"batch_id": "b1", "budget": 100})
    assert r.status_code == 201
    assert r.json() == {"batch_id": "b1", "budget": 100, "remaining": 100}


def test_duplicate_batch_rejected_and_original_untouched(client):
    assert client.post("/batches", json={"batch_id": "b1", "budget": 100}).status_code == 201
    r = client.post("/batches", json={"batch_id": "b1", "budget": 50})
    assert r.status_code == 409
    assert r.json()["code"] == "BATCH_ALREADY_EXISTS"
    assert client.get("/batches/b1").json()["budget"] == 100


@pytest.mark.parametrize("budget", [0, -1, -100])
def test_non_positive_budget_rejected(client, budget):
    r = client.post("/batches", json={"batch_id": "b1", "budget": budget})
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "payload",
    [
        {"budget": 10},                      # missing batch_id
        {"batch_id": "b1"},                  # missing budget
        {"batch_id": "", "budget": 10},      # empty batch_id
        {"batch_id": "b1", "budget": "ten"}, # wrong type
        {},                                  # empty body
    ],
)
def test_malformed_create_rejected(client, payload):
    r = client.post("/batches", json=payload)
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION_ERROR"


def test_get_batch(client):
    client.post("/batches", json={"batch_id": "b1", "budget": 7})
    r = client.get("/batches/b1")
    assert r.status_code == 200
    assert r.json() == {"batch_id": "b1", "budget": 7, "remaining": 7}


def test_get_unknown_batch_404(client):
    r = client.get("/batches/nope")
    assert r.status_code == 404
    assert r.json()["code"] == "BATCH_NOT_FOUND"
