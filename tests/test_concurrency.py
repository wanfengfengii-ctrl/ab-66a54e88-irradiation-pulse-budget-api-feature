import concurrent.futures as cf
import json
import threading

from fastapi.testclient import TestClient

THREADS = 20


def _post_authorization(app, barrier, payload):
    # One client per thread: an independent HTTP connection per request.
    client = TestClient(app)
    barrier.wait(timeout=10)
    return client.post("/authorizations", json=payload)


def _storm(app, payloads):
    barrier = threading.Barrier(len(payloads))
    with cf.ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        futures = [pool.submit(_post_authorization, app, barrier, p) for p in payloads]
        return [f.result() for f in futures]


def test_concurrent_authorizations_never_exceed_budget(app):
    TestClient(app).post("/batches", json={"batch_id": "race", "budget": 100})
    payloads = [
        {"batch_id": "race", "request_key": f"rk-{i}", "pulses": 10} for i in range(THREADS)
    ]

    responses = _storm(app, payloads)

    successes = [r for r in responses if r.status_code == 201]
    rejected = [r for r in responses if r.status_code == 409]
    assert len(successes) + len(rejected) == THREADS
    assert len(successes) == 10  # 100 / 10, never more
    assert all(r.json()["code"] == "INSUFFICIENT_BUDGET" for r in rejected)
    # Every 10-pulse step appears exactly once: no lost update, no double
    # deduction, balance never negative.
    assert sorted(r.json()["remaining"] for r in successes) == list(range(0, 100, 10))
    assert TestClient(app).get("/batches/race").json()["remaining"] == 0


def test_concurrent_same_key_is_deducted_once(app):
    TestClient(app).post("/batches", json={"batch_id": "same", "budget": 50})
    payload = {"batch_id": "same", "request_key": "shared-key", "pulses": 7}

    responses = _storm(app, [payload] * 12)

    assert all(r.status_code == 201 for r in responses)
    bodies = {json.dumps(r.json(), sort_keys=True) for r in responses}
    assert len(bodies) == 1  # every retry replays the one winning response
    assert TestClient(app).get("/batches/same").json()["remaining"] == 43


def test_concurrent_same_key_conflicting_fields_single_winner(app):
    TestClient(app).post("/batches", json={"batch_id": "conf", "budget": 50})
    payloads = [
        {"batch_id": "conf", "request_key": "shared", "pulses": p} for p in [5] * 6 + [9] * 6
    ]

    responses = _storm(app, payloads)

    wins = [r for r in responses if r.status_code == 201]
    conflicts = [r for r in responses if r.status_code == 409]
    # one pulses value wins the key; every request carrying that same value
    # replays the winner's response, so all 201 bodies are identical
    assert len({json.dumps(r.json(), sort_keys=True) for r in wins}) == 1
    winner_pulses = wins[0].json()["pulses"]
    assert len(wins) == sum(1 for p in payloads if p["pulses"] == winner_pulses)
    assert len(conflicts) == len(payloads) - len(wins)
    assert all(r.json()["code"] == "REQUEST_KEY_CONFLICT" for r in conflicts)
    # the key deducted exactly once, for the winning pulses value
    assert TestClient(app).get("/batches/conf").json()["remaining"] == 50 - winner_pulses


def test_concurrent_mixed_batches_and_retries(app):
    client = TestClient(app)
    client.post("/batches", json={"batch_id": "x", "budget": 80})
    client.post("/batches", json={"batch_id": "y", "budget": 80})
    payloads = []
    for i in range(8):
        payloads.append({"batch_id": "x", "request_key": f"xk-{i}", "pulses": 10})
        payloads.append({"batch_id": "y", "request_key": f"yk-{i}", "pulses": 10})
    payloads *= 2  # every request retried once, all keys duplicated in the storm

    responses = _storm(app, payloads)

    by_key = {}
    for r, p in zip(responses, payloads):
        assert r.status_code == 201
        by_key.setdefault(p["request_key"], set()).add(json.dumps(r.json(), sort_keys=True))
    # each key's duplicate responses are identical replays
    assert all(len(bodies) == 1 for bodies in by_key.values())
    assert TestClient(app).get("/batches/x").json()["remaining"] == 0
    assert TestClient(app).get("/batches/y").json()["remaining"] == 0
