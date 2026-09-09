# Standalone repository, HAMi and Kind verification

Date: 2026-09-09. This report extends the historical baseline in `verification.md`.

## Changes

- Imported the GRACE foundation at the new repository root, based on the user-specified initial commit `56e21eae268d222f6fc5b99456c89892eaab75d9`.
- Added distinct KAI accounting, HAMi memory and HAMi memory/SM capability contracts. Real runtime certification remains required; no caller flag establishes enforcement.
- Verified the integration against KAI's tagged v0.17.0 HAMi documentation. The binder owns the memory limit; GRACE supplies a separately certified integer SM cap where requested. Missing, stale, conflicting or unsupported evidence fails closed.
- Added a Mac/Linux existing-Kind runner, architecture-aware image build/load, owned dev/QA deployment and authenticated REST/gRPC smoke test.
- Added CI jobs for source/transport/Helm checks, image build, disposable PostgreSQL, and a disposable Kind cluster using the same existing-cluster test runner.

## Executed locally

**182 tests: 169 passed, 13 explicitly skipped, zero failures/errors.**

| Group | Passed | Scope |
|---|---:|---|
| Domain | 54 | Synthetic inventory, concurrency and lifecycle |
| Adapter | 42 | Translation and fake SDK, including 11 HAMi cases |
| Independent adversarial QA | 22 | Tenant, identity, ownership and release safety |
| REST/gRPC | 20 | Actual loopback listeners |
| Platform | 8 | Source and actual Helm render/lint guards |
| SQL contracts | 8 | Source assertions |
| Kind runner/client | 15 | CLI safety, command construction and actual loopback smoke/idle refusal |
| PostgreSQL runtime | 0 | 13 skipped; no disposable database available locally |

The complete suite used Python 3.12 and Helm 3.19.0:

```bash
GRACE_HELM_BIN=/path/to/helm python -m unittest discover -s tests -v
```

Python compilation, protobuf regeneration without drift, Bash syntax and Ruff checks `E9,F63,F7,F82` passed. These are separate from live deployment evidence.

`bash scripts/test-kind.sh --list` was attempted in this Linux environment and failed immediately with `Missing prerequisite: kind`. Docker, kubectl and Kind are not installed; no Mac connection or kubeconfig is configured. No cluster was modified by this local attempt.

## Executed in GitHub CI

[Run 34368785662](https://github.com/kiransgithub/grace/actions/runs/34368785662), source commit `3681b50b7284db8d93f04bffa6076bde19f8ebaa`, completed successfully on Linux GitHub-hosted runners.

| Job | Observed result |
|---|---|
| `test` | 182 discovered, 169 passed and 13 PostgreSQL skips; protobuf drift and compilation passed |
| `postgres` | All 13 previously skipped cases ran and passed on a fresh PostgreSQL 16 service; zero skips |
| `image` | Docker simulation image built successfully |
| `kind` | Built/loaded linux/amd64 image, installed parent Helm release in `grace-kind-dev`, and passed real REST/gRPC smoke through the pod |

Thus all **182 distinct automated test cases passed across the two test jobs**, with additional image and Kubernetes smoke evidence. Database tests verified migrations, transactional capacity, concurrent admission, rollback, expiry holds and release evidence. They do not establish durable runtime integration or DR.

The Kind runner discovered `kind-grace-ci`, tested authentication, shared REST/gRPC state, create/cancel idempotency, concurrent fractional no-overbooking, confirmed cleanup and full-device reuse. This cluster was created and destroyed by the dedicated CI action; the runner itself used an existing cluster. It did not access the user's Mac or demonstrate arm64 support in execution. Logs explicitly identify synthetic GPUs and no CUDA dispatch.

## Evidence that is still required

The Mac runner has **not run on the user's Mac**. Successful Linux CI cannot substitute for that Mac run. Follow `mac-kind.md` on the Mac with its existing Docker/Kind contexts, or provide an established remote execution connection.

Kind smoke verifies a synthetic allocator in a pod. It does not launch SkyPilot workloads or prove KAI/HAMi CUDA enforcement. Real NVIDIA hardware certification must verify library injection, actual over-budget CUDA allocation failure, configured SM throttling and protection against opt-out. See `hami.md`.

The service remains single-process, non-durable and dev/QA-only. PostgreSQL transaction tests do not wire persistence into the runtime. Production, HA, Okta, mandatory admission, telemetry and recovery gates remain open.
