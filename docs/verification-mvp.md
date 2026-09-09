# Clarified MVP verification

Date: 2026-09-09. This record extends the earlier HAMi/Kind baseline. Read
[MVP decisions](mvp-decisions.md) for accepted scope and remaining enterprise gates.

## Changes verified locally

- Strict/preferred/any location choice, approved/data-ready fallback, actual location in responses.
- Default best-effort queue, allocation-free waiting, separate queue/grant clocks, immediate queued cancellation, current-identity reauthorization and safe promotion.
- Equal priority by default; trusted project/BU/default rules; separate preemption and idle-reclamation exemptions; visible policy versions; refreshed pending policy and immutable active-grant snapshots.
- Compatible protobuf additions and REST/gRPC transport parity. User-supplied priority/exemption/administrator fields fail. Admin policy transport remains explicitly UNIMPLEMENTED.
- Simulator inventory now contains on-prem and two GCP locations, with Azure deferred.
- Kind smoke exercises actual queue promotion in addition to authenticated APIs, idempotency, concurrent no-overbooking and confirmed reuse.

**236 tests discovered: 223 passed, 13 PostgreSQL tests skipped locally, zero failures/errors.**

| Group | Passing local tests |
|---|---:|
| Existing domain | 54 |
| New MVP domain | 32 |
| New independent MVP safety | 17 |
| Existing adapter + HAMi | 42 |
| Existing independent safety | 22 |
| REST/gRPC | 25 |
| Kind helper/client | 15 |
| Platform/Helm | 8 |
| SQL source contracts | 8 |

Commands: `GRACE_HELM_BIN=/path/to/helm python -m unittest discover -s tests`;
Ruff `0.14.10` checks `E9,F63,F7,F82`. Both passed. The suite includes real loopback
listeners and the exact updated smoke client running with the synthetic observer.

Protobuf regeneration without drift, Python compilation, local Markdown links,
and the 41-task dependency tracker also passed verification.

## Executed in GitHub CI

[Run 34405230825](https://github.com/kiransgithub/grace/actions/runs/34405230825)
passed on source commit `8d466c99bda8610f1835a2d728ba598b3d09533e`.

| Job | Verified result |
|---|---|
| `test` | 223 passed; the 13 database cases skipped in this job |
| `postgres` | All 13 database cases passed on a fresh PostgreSQL 16 service |
| `image` | Updated simulation image built successfully |
| `kind` | Parent Helm deployment and actual REST/gRPC pod smoke passed, including queued promotion |

All **236 distinct automated cases passed across the test jobs**. The Kind client
filled four quarter shares, queued five additional requests without holds, cancelled
four waiting requests, released an active share and observed the remaining waiting
request receive capacity. It then verified cleanup and full-device reuse.

The PostgreSQL tests cover unchanged migrations `001`/`002`; they do not certify the
proposed migration `003` or wire durable persistence into the running service.
The CI cluster is Linux/amd64 Kind, not the user's Mac or an NVIDIA GPU estate.

## Limits

The queue and policy registry are in-memory. Restart loses state; only one simulator
replica is supported. Admin policy REST/gRPC, migration `003`, PostgreSQL runtime
adapter, Okta, real SkyPilot/KAI policy enforcement and DR remain implementation gates.
The new nullable queue timing and preferred-location API must not be forced into
migrations `001`/`002`; see [persistence specification](mvp-persistence-contract.md).

FIFO/backfill is scoped to tenant/environment, not a global cross-tenant scheduler.
Priority orders placement and does not itself preempt running work. Protected jobs
still obey owner cancellation and contractual expiry. Identity outages do not prove
revocation or authorize allocation. No unknown/live resource is freed by queue logic.

GTM/stateless APIs are the enterprise deployment target, not a claim about the current
simulation. PostgreSQL writer fencing, recovery of all durable control state, and
surviving workload reconciliation must pass before mutation readiness opens after DR.
The user's Mac and physical GPU/HAMi enforcement remain untested here.
