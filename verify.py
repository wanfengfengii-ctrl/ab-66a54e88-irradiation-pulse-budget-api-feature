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

        # --- Batch authorization review: snapshot-stable pagination ---
        page_batch = f"verify-pages-{run}"
        client.post("/batches", json={"batch_id": page_batch, "budget": 100})
        # A foreign-batch authorization created *before* the page batch's own
        # rows: its id is smaller than the page-batch cap, which is the only
        # shape that exercises the "position belongs to another batch" rule.
        foreign_batch = f"verify-foreign-{run}"
        client.post("/batches", json={"batch_id": foreign_batch, "budget": 50})
        foreign = client.post(
            "/authorizations",
            json={"batch_id": foreign_batch, "request_key": f"fgn-{run}", "pulses": 5},
        )
        foreign_id = foreign.json()["authorization_id"]
        # 5 authorizations of 10 pulses -> ids may not be contiguous globally,
        # so every comparison uses the ids returned by the API.
        page_ids = []
        for i in range(5):
            rr = client.post(
                "/authorizations",
                json={"batch_id": page_batch, "request_key": f"pg-{run}-{i}", "pulses": 10},
            )
            check(f"page-fixture authorize {i} -> 201", rr.status_code == 201, rr.text)
            page_ids.append(rr.json()["authorization_id"])

        first_page = client.get(f"/batches/{page_batch}/authorizations?page_size=2")
        check("review first page -> 200", first_page.status_code == 200, first_page.text)
        fb = first_page.json()
        check("review first page has 2 items in id order", [it["authorization_id"] for it in fb["items"]] == page_ids[:2], first_page.text)
        check("review pins snapshot_max_id", fb["snapshot_max_id"] == page_ids[-1], first_page.text)
        check("review used_pulses covers whole snapshot", fb["used_pulses"] == 50, first_page.text)
        check("review snapshot_remaining", fb["snapshot_remaining"] == 50, first_page.text)
        check("review snapshot_budget", fb["snapshot_budget"] == 100, first_page.text)

        # A new authorization is committed while the reviewer is paging.
        between = client.post(
            "/authorizations",
            json={"batch_id": page_batch, "request_key": f"pg-mid-{run}", "pulses": 30},
        )
        check("inter-page authorize -> 201", between.status_code == 201, between.text)

        seen = list(fb["items"])
        body = fb
        page_count = 1
        while body.get("next_position") is not None:
            nxt = client.get(
                f"/batches/{page_batch}/authorizations"
                f"?page_size=2&snapshot_max_id={body['snapshot_max_id']}&position={body['next_position']}"
            )
            check(f"review page {page_count + 1} -> 200", nxt.status_code == 200, nxt.text)
            body = nxt.json()
            check(
                f"review page {page_count + 1} keeps pinned cap",
                body["snapshot_max_id"] == fb["snapshot_max_id"],
                nxt.text,
            )
            check(
                f"review page {page_count + 1} keeps pinned statistics",
                body["used_pulses"] == 50 and body["snapshot_remaining"] == 50,
                nxt.text,
            )
            seen.extend(body["items"])
            page_count += 1
        check("multi-page review returns 3 pages", page_count == 3, str(page_count))
        check(
            "inter-page authorization never mixes into the run",
            [it["authorization_id"] for it in seen] == page_ids
            and all(it["authorization_id"] != between.json()["authorization_id"] for it in seen),
            str([it["authorization_id"] for it in seen]),
        )
        check("review ends without a cursor", body["next_position"] is None, str(body.get("next_position")))

        # A new run started afterwards does include the inter-page row.
        fresh = client.get(f"/batches/{page_batch}/authorizations?page_size=20").json()
        check(
            "fresh review run sees the inter-page authorization and updated stats",
            [it["authorization_id"] for it in fresh["items"]] == page_ids + [between.json()["authorization_id"]]
            and fresh["used_pulses"] == 80
            and fresh["snapshot_remaining"] == 20,
            str(fresh),
        )

        # Empty batch: zero rows, cap 0, statistics reflect the untouched budget.
        empty_batch = f"verify-empty-{run}"
        client.post("/batches", json={"batch_id": empty_batch, "budget": 7})
        eb = client.get(f"/batches/{empty_batch}/authorizations")
        check("empty batch review -> 200", eb.status_code == 200, eb.text)
        check(
            "empty batch review shape",
            eb.json() == {
                "batch_id": empty_batch,
                "items": [],
                "used_pulses": 0,
                "snapshot_remaining": 7,
                "snapshot_budget": 7,
                "snapshot_max_id": 0,
                "next_position": None,
            },
            eb.text,
        )

        # Illegal pagination parameters all return the stable machine code.
        def is_pagination_error(rr: httpx.Response) -> bool:
            return rr.status_code == 400 and rr.json().get("code") == "PAGINATION_ERROR"

        check("page_size=0 -> 400 PAGINATION_ERROR", is_pagination_error(client.get(f"/batches/{page_batch}/authorizations?page_size=0")))
        check("page_size=101 -> 400 PAGINATION_ERROR", is_pagination_error(client.get(f"/batches/{page_batch}/authorizations?page_size=101")))
        check("position without cap -> 400 PAGINATION_ERROR", is_pagination_error(client.get(f"/batches/{page_batch}/authorizations?position=1")))
        check("cap without position -> 400 PAGINATION_ERROR", is_pagination_error(client.get(f"/batches/{page_batch}/authorizations?snapshot_max_id=1")))
        check(
            "cap from another batch -> 400 PAGINATION_ERROR",
            is_pagination_error(
                client.get(
                    f"/batches/{page_batch}/authorizations"
                    f"?snapshot_max_id={foreign_id}&position={page_ids[0]}"
                )
            ),
        )
        check(
            "illegal position (not in batch) -> 400 PAGINATION_ERROR",
            is_pagination_error(
                client.get(
                    f"/batches/{page_batch}/authorizations"
                    f"?snapshot_max_id={page_ids[-1]}&position={foreign_id}"
                )
            )
            or is_pagination_error(
                client.get(
                    f"/batches/{page_batch}/authorizations"
                    f"?snapshot_max_id={page_ids[-1]}&position=99999999"
                )
            ),
        )
        check(
            "review on unknown batch keeps 404 BATCH_NOT_FOUND",
            client.get(f"/batches/missing-{run}/authorizations").status_code == 404
            and client.get(f"/batches/missing-{run}/authorizations").json().get("code") == "BATCH_NOT_FOUND",
        )

        # Review (including malformed calls) is read-only: budget stays at 20
        # after the inter-page 30-pulse authorization, and deductions work.
        balance_after_review = client.get(f"/batches/{page_batch}")
        check(
            "review never moved the balance",
            balance_after_review.json().get("remaining") == 20,
            balance_after_review.text,
        )
        final_ok = client.post(
            "/authorizations",
            json={"batch_id": page_batch, "request_key": f"pg-last-{run}", "pulses": 20},
        )
        check("deduction still works after review errors", final_ok.status_code == 201 and final_ok.json().get("remaining") == 0, final_ok.text)
        final_bad = client.post(
            "/authorizations",
            json={"batch_id": page_batch, "request_key": f"pg-over-{run}", "pulses": 1},
        )
        check("budget safety preserved after review", final_bad.status_code == 409 and final_bad.json().get("code") == "INSUFFICIENT_BUDGET", final_bad.text)

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("\nAll acceptance checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
