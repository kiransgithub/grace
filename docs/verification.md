# Verification evidence and release limits

**Latest extension:** [Standalone repository, HAMi and Kind verification](verification-hami-mac.md) records all 182 distinct tests passing across CI jobs, a successful Docker build and deployed Kind pod smoke. The 156-test results and unexecuted-PostgreSQL statements below are the preserved earlier foundation baseline, superseded only within the evidence scope of that extension.

This report distinguishes checks actually executed from design requirements. A green
unit test or rendered Kubernetes manifest does not certify a real GPU service.

## Scope

The initial deliverable is a development safety foundation: API contracts,
simulation-backed domain behavior, an experimental PostgreSQL schema, execution
adapter boundaries, and deployment source. Production remains disabled. Real
Okta authentication, physical fractional GPUs, cloud/Kubernetes enforcement,
multi-replica persistent APIs, and disaster recovery require their own acceptance
gates.

## Executed checks

Final integrated verification executed by the independent QA agent on 2026-09-09:

```bash
cd grace
# Use an approved Helm 3 executable; Helm 3.19.0 was used for this run.
GRACE_HELM_BIN=/path/to/approved/helm PYTHONPATH=src \
  python -m unittest discover -s tests
```

Result: **156 tests discovered: 143 passed; 13 PostgreSQL integration tests
skipped; 0 failures/errors**. Runtime was 1.102 seconds on the task's development
environment; that is not a throughput or service-latency benchmark. The interpreter
used here was Python 3.12.14.

| Test group | Passed | Skipped | Evidence scope |
|---|---:|---:|---|
| Domain | 54 | 0 | Single-process simulation, including 1,000 competing requests and 1,000 duplicate retries |
| SkyPilot/KAI adapters | 31 | 0 | Pure translation and fake-SDK faults, not live GPU execution |
| Independent adversarial safety tests | 22 | 0 | Tenant/subject boundaries, release ordering, malformed flags, cluster identity and DR epochs |
| REST/gRPC | 20 | 0 | Actual loopback HTTP/gRPC against the shared simulation service |
| Platform | 8 | 0 | Source checks plus actual Helm lint/render and unsafe-value rejection |
| Schema source contracts | 8 | 0 | Structural assertions only |
| PostgreSQL integration | 0 | 13 | Not executed locally; requires explicit disposable database |

Independent tests in `test_system_safety.py` and
`test_system_adapter_safety.py` cover tenant and subject isolation,
all-or-nothing device admission, conservative observed/ledger occupancy,
cancellation races, expired/unknown holds, same-region distinct-cluster
separation, strict boolean authorization, ordered release evidence, and rejection
of pre-recovery evidence after a DR epoch changes.

Both SQL migrations also passed top-level PostgreSQL syntax parsing with
`pglast==7.12`; the second migration's ten PL/pgSQL function bodies passed its
procedural-language parser too. Parsing does **not** execute those functions or
resolve their database objects, foreign keys, triggers, transactions, or
concurrency. It is not a successful migration test.

**PostgreSQL runtime tests were not executed.** A packaged disposable PostgreSQL
runtime could be downloaded, but this environment would not permit the required
unprivileged database process. Attempts stopped at the permission boundary; no
external database was contacted. The opt-in database tests must run in CI against
an explicitly provided disposable database.

The final migration syntax check parsed 47 statements in migration 001 and 24
statements in migration 002. A separate static AST check confirmed that all 54
declared foreign-key target column tuples match declared primary/unique keys.
These results do not replace running migrations on PostgreSQL.

The complete integrated result is also recorded in `docs/delivery/status.md`.
The integration lead separately confirmed protobuf regeneration with
`grpcio-tools==1.78.0`, editable package installation, Python compilation,
Ruff 0.14.10 checks `E9,F63,F7,F82`, and validation of the 39-task dependency
tracker. Those checks passed; the PostgreSQL CI job itself was not run here.

## Independent review findings addressed

- A pre-stop snapshot with the same timestamp as cancellation could be mistaken
  for release evidence. The domain now requires a strictly newer observation.
- Geographical location alone could combine devices from different Kubernetes
  clusters. Explicit cluster identity now participates in fitting a device group.
- String values such as `"false"` could become truthy authorization/certification
  flags. Adapter contracts now require actual booleans, and domain read/cancel
  visibility requires the controller flag to be exactly `True`.
- Remote release evidence did not establish event order. It now requires a fresh
  authoritative absence observation strictly newer than both ownership fencing
  and stop intent, without imposing a mutual order on those two timestamps.
  Dispatch must also be affirmatively reconciled; unknown dispatch outcomes keep
  the hold until the trusted controller resolves them.
- Container-valued environment fields could raise an internal exception instead
  of a validation error. They now fail before capacity evaluation.

Regression tests cover these findings. These are local reference-contract
fixes, not evidence that a deployed admission controller enforces them.

SQL source review additionally tightened same-reservation foreign keys, effective
fractional memory budgets, dispatch-time policy rechecks, and release evidence
requiring current ownership and a fresh complete post-stop/post-fence snapshot.
These SQL fixes have syntax/source-contract evidence only until the disposable
PostgreSQL gate runs.

## Required integration gates

| Gate | Required evidence | Initial status |
|---|---|---|
| Atomic reservation | Real PostgreSQL concurrent allocations, full rollback, correct disjoint-time accounting | 13 opt-in PostgreSQL scenarios skipped; persistence gate remains open |
| Retry safety | Same scoped idempotency key/payload yields one reservation; conflicting payload rejected | Local tests; durable DB gate not run |
| Tenant isolation | Different authenticated tenant cannot read, cancel, change, or adopt a reservation | Independent local tests passed; live identity/admission not run |
| Release safety | Remote uncertainty, active workloads, and missing observations retain capacity holds | Independent local contract tests passed; real termination not run |
| Transport parity | HTTP and gRPC encode the same domain errors and tenancy policy | 20 loopback transport/supervision tests passed |
| Deployment | Helm render/lint and one-replica simulation fail-closed checks | Helm render/lint passed; image build, pod startup and cluster validation not run |
| Actual execution | Real SkyPilot launch/cancel on an existing KAI GPU cluster | Not run: no authorized cluster access |
| Fractional GPUs | Physical multi-tenant scheduling, real memory behavior, telemetry, cancellation | Not run: no physical GPU access |
| Mandatory gateway | Forged metadata, token replay, direct pod/Job/VM launch are rejected | Not run: requires real IAM/admission setup |
| Identity | Okta issuer/audience/JWKS rotation and AD group mappings | Not run: no configured enterprise identity |
| HA and DR | PostgreSQL failover, backup restore, old-writer fencing, surviving-workload reconciliation | Not run: requires deployed stateful topology |
| Cost/showback | Immutable usage deduplication and approved rate attribution reconciled against actual billing | Not run: no billing integration |

## Failure cases that must remain fail-closed

- Stale or missing inventory must not create promiseable capacity.
- An HTTP/RPC timeout after submission must not be treated as evidence that no
  workload exists.
- Expiry or cancellation does not free a device until absence or effective
  execution fencing has been established.
- A request-level production flag never creates production authorization.
- A future reservation must not sum non-overlapping claims as though simultaneous,
  nor permit partial overlap to exceed a physical device's fractional or memory
  capacity.
- A control-plane restore must not silently reuse tokens or release capacity still
  used by surviving remote workloads.
- A database transaction lock does not itself enforce a lease against a running
  Kubernetes pod; target-side admission and reconciliation remain separate gates.

## Reproducing checks

Use the commands in the project README and the acceptance records in
`docs/delivery/status.md`. Optional integration tests require an explicitly
configured disposable PostgreSQL database; they must never target production.
Skipped tests are not successful tests and must be reported as skipped.

The database test owner must create a **new empty disposable PostgreSQL 16+**
database and then explicitly opt in:

```bash
export GRACE_TEST_POSTGRES_DISPOSABLE=1
export GRACE_TEST_POSTGRES_DSN='postgresql://test-user:placeholder@127.0.0.1:5432/grace_disposable_test'
PYTHONPATH=src python -m unittest discover -s tests -p 'test_schema*.py' -v
```

The example credential is a placeholder. Both environment variables and `psycopg`
are required. The harness refuses an existing `grace` schema, installs migrations
and test fixtures, and does not drop an existing database/schema. Its CI job is
source configuration only until an actual pipeline run supplies evidence.
