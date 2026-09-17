"""One-shot acceptance checks against a running API.

Used by the `verify` docker-compose service, but also runnable directly:

    python verify.py                 # targets http://localhost:${API_PORT:-8000}
    API_BASE_URL=http://host:port python verify.py

Exits 0 when every check passes, 1 otherwise.
"""
from __future__ import annotations

import concurrent.futures as cf
import os
import sys
import time
import uuid

import httpx

BASE_URL = os.environ.get("API_BASE_URL") or f"http://localhost:{os.environ.get('API_PORT', '8000')}"
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def wait_for_api(client: httpx.Client, attempts: int = 60) -> None:
    for _ in range(attempts):
        try:
            if client.get("/health").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise SystemExit("API did not become healthy in time")


def main() -> int:
    # Unique ids per run so the script is re-runnable against a persistent DB.
    run = uuid.uuid4().hex[:12]
    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        wait_for_api(client)

        batch = f"verify-{run}"
        r = client.post("/batches", json={"batch_id": batch, "budget": 100})
        check("create batch returns 201", r.status_code == 201, r.text)
        check("create batch echoes budget and balance", r.json().get("budget") == 100 and r.json().get("remaining") == 100, r.text)

        r = client.post("/batches", json={"batch_id": batch, "budget": 100})
        check("duplicate batch -> 409 BATCH_ALREADY_EXISTS", r.status_code == 409 and r.json().get("code") == "BATCH_ALREADY_EXISTS", r.text)

        payload = {"batch_id": batch, "request_key": f"key-a-{run}", "pulses": 40}
        first = client.post("/authorizations", json=payload)
        check("authorize returns 201", first.status_code == 201, first.text)
        check("authorize returns authorization id", isinstance(first.json().get("authorization_id"), int), first.text)
        check("authorize returns post-deduction balance", first.json().get("remaining") == 60, first.text)

        replay = client.post("/authorizations", json=payload)
        check("retry replays 201 with identical body", replay.status_code == 201 and replay.json() == first.json(), replay.text)

        conflict = client.post("/authorizations", json={**payload, "pulses": 41})
        check("same key, different pulses -> 409 REQUEST_KEY_CONFLICT", conflict.status_code == 409 and conflict.json().get("code") == "REQUEST_KEY_CONFLICT", conflict.text)

        insufficient = client.post("/authorizations", json={"batch_id": batch, "request_key": f"key-b-{run}", "pulses": 1000})
        check("insufficient budget -> 409 INSUFFICIENT_BUDGET", insufficient.status_code == 409 and insufficient.json().get("code") == "INSUFFICIENT_BUDGET", insufficient.text)
        check("failed request left no ledger record", client.get(f"/authorizations/key-b-{run}").status_code == 404)

        missing = client.post("/authorizations", json={"batch_id": f"missing-{run}", "request_key": f"key-c-{run}", "pulses": 1})
        check("unknown batch -> 404 BATCH_NOT_FOUND", missing.status_code == 404 and missing.json().get("code") == "BATCH_NOT_FOUND", missing.text)

        invalid = client.post("/authorizations", json={"batch_id": batch, "request_key": f"key-d-{run}", "pulses": 0})
        check("non-positive pulses -> 422 VALIDATION_ERROR", invalid.status_code == 422 and invalid.json().get("code") == "VALIDATION_ERROR", invalid.text)

        # Concurrency: 20 parallel requests of 10 pulses against a budget of 100.
        race_batch = f"verify-race-{run}"
        client.post("/batches", json={"batch_id": race_batch, "budget": 100})

        def fire(i: int) -> httpx.Response:
            with httpx.Client(base_url=BASE_URL, timeout=10) as c:
                return c.post("/authorizations", json={"batch_id": race_batch, "request_key": f"race-{i}-{run}", "pulses": 10})

        with cf.ThreadPoolExecutor(max_workers=20) as pool:
            responses = list(pool.map(fire, range(20)))
        wins = [r for r in responses if r.status_code == 201]
        check("concurrent successes never exceed budget", len(wins) == 10, f"got {len(wins)} successes")
        check(
            "concurrent balances are unique steps down to zero",
            sorted(r.json()["remaining"] for r in wins) == list(range(0, 100, 10)),
            str(sorted(r.json().get("remaining", -1) for r in wins)),
        )
        balance = client.get(f"/batches/{race_batch}")
        check("final balance is exactly zero", balance.status_code == 200 and balance.json().get("remaining") == 0, balance.text)

        # Concurrency: a storm of identical retries deducts exactly once.
        storm_batch = f"verify-storm-{run}"
        client.post("/batches", json={"batch_id": storm_batch, "budget": 50})
        storm_payload = {"batch_id": storm_batch, "request_key": f"storm-{run}", "pulses": 7}

        def fire_same(_: int) -> httpx.Response:
            with httpx.Client(base_url=BASE_URL, timeout=10) as c:
                return c.post("/authorizations", json=storm_payload)

        with cf.ThreadPoolExecutor(max_workers=10) as pool:
            storm = list(pool.map(fire_same, range(10)))
        check("same-key storm: every retry gets 201", all(r.status_code == 201 for r in storm))
        check("same-key storm: identical replayed bodies", len({r.text for r in storm}) == 1)
        check(
            "same-key storm: deducted exactly once",
            client.get(f"/batches/{storm_batch}").json().get("remaining") == 43,
        )

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("\nAll acceptance checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
