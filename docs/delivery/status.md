# GRACE delivery status

**Standalone/HAMi/Kind update — 2026-09-09:** All 182 distinct automated tests passed across CI jobs (169 standard tests plus 13 real PostgreSQL tests). Docker image build and Kubernetes pod REST/gRPC smoke also passed. The new repository includes HAMi capability contracts and an existing-Kind Mac/Linux runner. See [latest evidence](../verification-hami-mac.md). The 156-test table below is the historical foundation baseline. Mac, physical CUDA/HAMi, runtime persistence, enterprise HA and DR remain unverified.

**Release 0.1: detailed design and locally tested development foundation delivered. Not a production GPU service.**

M0 design is ready for enterprise review. M1 delivers a modular, single-process, non-durable simulation with real REST/gRPC transports, guarded SkyPilot adapter source, PostgreSQL design, and parent/child Kubernetes Helm charts. M2 real infrastructure integration is the next critical milestone. Existing demonstration code is preserved.

## Final verification — 2026-09-09

**156 tests discovered: 143 passed, 13 explicitly skipped, zero failures/errors.** Root and independent QA confirmed the combined result. Skips are PostgreSQL runtime tests, not successful database tests.

| Test group | Passed | Scope |
|---|---:|---|
| Domain | 54 | In-memory fractional admission/lifecycle; includes 1,000 competing claims and 1,000 idempotent retries |
| SkyPilot adapter | 31 | Fake SDK; translation, uncertainty, cancellation, epoch and release checks |
| Independent adversarial QA | 22 | Tenant/subject isolation, conservative occupancy, cluster identity, malformed values and release evidence |
| HTTP/gRPC transports | 20 | Actual loopback listeners and generated client against the shared simulation engine |
| Platform | 8 | Source checks plus actual Helm lint/render/guards; includes 11 unsafe override subcases |
| SQL source contracts | 8 | Schema/source assertions, not database execution |
| PostgreSQL runtime | 0 | 13 scenarios skipped because a disposable database process could not be run here |

Additional checks passed:

- Editable package installation and regeneration using `grpcio-tools==1.78.0`.
- Python compilation and Ruff `0.14.10` checks `E9,F63,F7,F82`.
- Checksum-verified official Helm `3.19.0` rendering/lint and unsafe-configuration rejection.
- `pglast==7.12` parsed migration 001 (47 statements), migration 002 (24 statements), and 10 PL/pgSQL function bodies; 54 foreign-key targets matched unique keys in AST checks.
- The 39-task tracker has valid owners/statuses, no duplicate or missing dependency IDs, and an acyclic dependency graph.

The combined command ran from `grace/` with `GRACE_HELM_BIN` set to the verified Helm executable:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Full original command, interpreter path and evidence scopes are retained in `backlog.json`. Reproduce dependency setup using the main README. Optional Helm tests need a real configured Helm executable; PostgreSQL tests need an explicitly approved disposable database. Skipped prerequisites must remain visible in results.

Release evidence was specifically tested to require an authoritative absence observation newer than both the stop intent and execution fence, plus reconciled dispatch. The stop and fence need not occur in one particular order. This is a tested reference contract, not deployed target-side enforcement.

## Delivered artifacts

| Deliverable | Location | Readiness |
|---|---|---|
| Architecture and execution sequence | `docs/execution-plan.md`, `docs/delivery/execution-plan.md` | Reviewed source design; enterprise acceptance pending |
| Optimized ER and transactional SQL | `docs/data-model.md`, `migrations/` | All 13 disposable PostgreSQL runtime scenarios passed in CI; application persistence wiring remains open |
| gRPC and REST contract | `proto/grace/v1/grace.proto`, `docs/api-design.md` | Generated protobuf and tested implemented transports; no OpenAPI artifact claimed |
| Domain safety kernel | `src/grace/domain/` | Locally tested; synthetic inventory and process-local state |
| SkyPilot/KAI adapter boundary | `src/grace/adapters/`, `docs/skypilot-kai.md` | Fake-SDK tested; experimental fractions default disabled |
| Kubernetes parent and child modules | `deploy/helm/grace/` | Helm checks, Docker build and actual Linux Kind simulation pod smoke passed in CI |
| Recovery/security runbooks | `docs/operations.md` | Design source; no live enforcement or restore drill |
| Project-management record | `docs/delivery/backlog.json` | 40 tasks with owners, dependencies, criteria and evidence; GRACE-014 tracks blocked Mac execution |
| Independent verification | `docs/verification.md` | Findings, corrected regressions and remaining gates |

## Important release limits

- The executable loses reservations on restart. One pod/process is mandatory; multiple replicas are intentionally rejected. Optional PostgreSQL pods do not make it durable.
- Create/Get/List/Cancel run against synthetic inventory. Renewal, activation, watch, live cluster APIs and usage services are contract-only or explicitly unimplemented.
- No real GPU workload is launched, no cloud cluster is changed, and no email is sent.
- AD/Okta integration, target-side admission/fencing, physical KAI sharing, telemetry/showback and durable reconciliation remain future gates.
- All 13 PostgreSQL scenarios passed in CI. Runtime persistence integration, process restart recovery and restore drills still need implementation and evidence.
- Docker build and a real Linux Kind simulation pod passed in CI. Mac/arm64, PowerShell helpers, physical GPU integration, HA and DR remain unverified.
- Production is disabled at runtime and deployment. Future support needs a global gate, authorized application/requester and per-request opt-in; no flag can currently enable it.
- Fractions are scheduling/accounting shares. Certified KAI/HAMi runtimes can additionally enforce CUDA memory and SM-utilization caps; these do not provide guaranteed throughput or hardware fault isolation. Physical enforcement is not certified by this release.

## Next dependency-ready execution

1. **GRACE-101** — Integrate transactional PostgreSQL into the runtime, retain the passing database scenarios and add durable restart/idempotency evidence; do not deploy multiple API replicas first.
2. **GRACE-102/107** — Implement durable outbox dispatch and reconciliation with ambiguity, epoch and confirmed-release safety.
3. **GRACE-103/104/105** — Certify an actual KAI cluster, configure Okta/AD and prove non-bypass admission/RBAC/IAM.
4. **GRACE-106** — Verify real SkyPilot-to-KAI fractional visibility, accounting, cancellation and cleanup before enabling experimental fractions.
5. Proceed to multi-cluster QA, showback, workload-aware reaping and recovery drills. Production remains behind M5 acceptance.

Core proposals: dev/QA control API 99.9%; regional synchronous DB RPO 0/RTO up to 5 minutes; site recovery RPO up to 5 minutes/RTO up to 60 minutes. These are not measured guarantees. Service RTO includes ownership reconciliation and safe reopening; a fast database restart alone does not satisfy it. See the execution blueprint and operations runbook for all stateful and application-data recovery targets.

## Agent continuity and external dependencies

The project-management, domain, data, platform, integration, transport and independent-QA agents performed bounded work in this session. They are not persistent workers or recurring automations. No external project issues were created. Resume from the earliest dependency-ready backlog item and append new evidence.

Enterprise owners must supply real cluster access, approved SkyPilot versions/capabilities, Okta registration and roles, admission/IAM administration, secret management, backup/restore targets, verified notification destinations and financial rate ownership before the respective end-to-end gates can pass.
