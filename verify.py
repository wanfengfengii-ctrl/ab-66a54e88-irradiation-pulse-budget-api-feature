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

        # --- Batch authorization detail listing with a pinned snapshot ---
        page_batch = f"verify-page-{run}"
        client.post("/batches", json={"batch_id": page_batch, "budget": 1000})
        for i in range(5):
            client.post(
                "/authorizations",
                json={"batch_id": page_batch, "request_key": f"pg-{i}-{run}", "pulses": 10},
            )

        def fetch_page(page_size, position=0, snapshot_max_id=None):
            params = {"page_size": page_size}
            if position:
                params["position"] = position
            if snapshot_max_id is not None:
                params["snapshot_max_id"] = snapshot_max_id
            return client.get(f"/batches/{page_batch}/authorizations", params=params)

        p1 = fetch_page(2)
        check("listing first page -> 200", p1.status_code == 200, p1.text)
        p1j = p1.json()
        check("listing first page has 2 ascending items", [it["authorization_id"] for it in p1j["items"]] == sorted(it["authorization_id"] for it in p1j["items"]) and len(p1j["items"]) == 2, p1.text)
        check("listing pins snapshot_max_id", isinstance(p1j["snapshot_max_id"], int), p1.text)
        check("listing stats on first page", p1j["used_pulses"] == 50 and p1j["snapshot_remaining"] == 950 and p1j["snapshot_budget"] == 1000, p1.text)
        check("listing first page points to next position", p1j["next_position"] == 2 and p1j["has_more"] is True, p1.text)

        # New authorizations land between page 1 and page 2: the pinned view
        # must not mix them in or drop any of the original five.
        for i in range(3):
            client.post(
                "/authorizations",
                json={"batch_id": page_batch, "request_key": f"pg-late-{i}-{run}", "pulses": 1},
            )
        p2 = fetch_page(2, position=p1j["next_position"], snapshot_max_id=p1j["snapshot_max_id"])
        check("listing second page -> 200", p2.status_code == 200, p2.text)
        p2j = p2.json()
        check("listing keeps same snapshot while paging", p2j["snapshot_max_id"] == p1j["snapshot_max_id"], p2.text)
        p3 = fetch_page(2, position=p2j["next_position"], snapshot_max_id=p1j["snapshot_max_id"])
        p3j = p3.json()
        seen = [it["authorization_id"] for it in p1j["items"] + p2j["items"] + p3j["items"]]
        check("listing walk covers exactly the original 5, in order", seen == list(range(seen[0], seen[0] + 5)), str(seen))
        check("listing ends with no next position", p3j["next_position"] is None and p3j["has_more"] is False, p3.text)
        check("listing stats stay frozen at the snapshot", p3j["used_pulses"] == 50 and p3j["snapshot_remaining"] == 950, p3.text)

        # A fresh first request observes the current state incl. late inserts.
        fresh = fetch_page(50).json()
        check("fresh listing sees later authorizations", len(fresh["items"]) == 8 and fresh["used_pulses"] == 53 and fresh["snapshot_remaining"] == 947, str(fresh))

        # Empty batch.
        empty_batch = f"verify-empty-{run}"
        client.post("/batches", json={"batch_id": empty_batch, "budget": 9})
        er = client.get(f"/batches/{empty_batch}/authorizations", params={"page_size": 10})
        check("empty batch listing -> 200 with empty items", er.status_code == 200 and er.json()["items"] == [] and er.json()["snapshot_max_id"] is None and er.json()["used_pulses"] == 0 and er.json()["snapshot_remaining"] == 9, er.text)

        # Error surface: unknown batch keeps BATCH_NOT_FOUND; bad paging params
        # are a stable PAGINATION_ERROR and never change balances.
        nf = client.get(f"/batches/missing-{run}/authorizations", params={"page_size": 10})
        check("listing unknown batch -> 404 BATCH_NOT_FOUND", nf.status_code == 404 and nf.json().get("code") == "BATCH_NOT_FOUND", nf.text)
        for params, label in (
            ({"page_size": 0}, "page_size=0"),
            ({"page_size": 201}, "page_size=201"),
            ({"page_size": 2, "position": 1}, "position without snapshot"),
            ({"page_size": 2, "position": 999, "snapshot_max_id": p1j["snapshot_max_id"]}, "position past end"),
            ({"page_size": 2, "position": 0, "snapshot_max_id": 999_999_999}, "foreign snapshot id"),
        ):
            rr = client.get(f"/batches/{page_batch}/authorizations", params=params)
            check(f"listing bad params ({label}) -> 400 PAGINATION_ERROR", rr.status_code == 400 and rr.json().get("code") == "PAGINATION_ERROR", rr.text)
        check(
            "listing errors left the balance untouched",
            client.get(f"/batches/{page_batch}").json().get("remaining") == 947,
        )
        deduct_after = client.post(
            "/authorizations",
            json={"batch_id": page_batch, "request_key": f"pg-after-{run}", "pulses": 3},
        )
        check("deductions still work after listing errors", deduct_after.status_code == 201 and deduct_after.json().get("remaining") == 944, deduct_after.text)

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("\nAll acceptance checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
