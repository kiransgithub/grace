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

## Evidence that is still required

The Mac runner has **not run on the user's Mac**. GitHub CI, when executed, is Linux evidence and cannot substitute for that Mac run. Follow `mac-kind.md` on the Mac with its existing Docker/Kind contexts, or provide an established remote execution connection.

Kind smoke verifies a synthetic allocator in a pod. It does not launch SkyPilot workloads or prove KAI/HAMi CUDA enforcement. Real NVIDIA hardware certification must verify library injection, actual over-budget CUDA allocation failure, configured SM throttling and protection against opt-out. See `hami.md`.

The service remains single-process, non-durable and dev/QA-only. PostgreSQL transaction tests do not wire persistence into the runtime. Production, HA, Okta, mandatory admission, telemetry and recovery gates remain open.
