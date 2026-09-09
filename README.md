# GRACE — GPU Reservation, Allocation & Control Engine

**Release 0.1: execution blueprint and tested development safety foundation.**

[Latest verification](docs/verification-mvp.md): the clarified MVP adds location modes,
best-effort queueing and visible admin policy. Historical image, PostgreSQL and Kind
evidence is retained separately. The user's Mac and physical GPU/HAMi remain unverified.

Existing Kubernetes GPU estates • SkyPilot execution boundary • KAI/HAMi fractional sharing
• AD/Okta target • dev/QA first • showback first • DR by design.

This is the standalone `kiransgithub/grace` project, imported from the tested
GRACE foundation in `kiransgithub/gpu-scheduler`. The original laptop demo remains
in that separate repository. Commands here run from this repository's root.

## Read first

The executable in this release is **a single-process, non-durable simulation**. REST
and gRPC are real and share one tested allocation engine, but their inventory is
synthetic. Restarting discards reservations. No running workload is dispatched,
no production access is possible, no real email is sent, and no live GPU/Okta/DR
certification is claimed. PostgreSQL migrations are provided separately; the runtime
repository/outbox/reconciler implementation is the next milestone, not secretly
replaced by in-memory state.

| Deliverable | Entry point |
|---|---|
| Detailed architecture, decisions and delivery sequence | [Execution blueprint](docs/execution-plan.md) |
| Latest confirmed MVP scope and policy choices | [MVP decisions](docs/mvp-decisions.md) |
| Optimized relational design, ER diagrams, locking invariants | [Data model](docs/data-model.md) |
| Versioned protocol and REST mappings | [API design](docs/api-design.md), [protobuf](proto/grace/v1/grace.proto) |
| Project-manager backlog, owners, dependencies and acceptance gates | [Delivery status](docs/delivery/status.md), [backlog](docs/delivery/backlog.json) |
| Fractional integration and cluster certification | [SkyPilot/KAI boundary](docs/skypilot-kai.md) |
| CUDA memory and compute-limit enforcement | [KAI/HAMi capability contract](docs/hami.md) |
| Existing Mac/Linux Kind cluster tests | [Mac Kind runner](docs/mac-kind.md) |
| Stateful DR catalogue and security/operations | [Operations](docs/operations.md) |
| Executed tests and explicit unverified areas | [Latest verification](docs/verification-mvp.md), [original baseline](docs/verification.md) |
| Parent Helm chart with child modules | [Kubernetes delivery](deploy/README.md) |

## Design decisions

- GPU fractions use integers: 250 millicards = 0.25 GPU admission/memory share.
  KAI alone accounts for shares. With its HAMi plugin and resource isolator,
  certified CUDA workloads can have software-enforced memory limits; HAMi-core
  also supports SM-utilization limits when configured and validated. These are
  caps, not a throughput guarantee or MIG hardware isolation. See [HAMi details](docs/hami.md).
- Users choose `location_policy`: `strict`, `preferred` with approved fallback, or
  `any`. MVP infrastructure is existing on-prem Kubernetes and multi-region GKE.
  Data access and `dev`/`qa`/`prod` boundaries apply to every placement.
- Best-effort queueing is the default. Waiting requests hold no GPUs, have a separate
  queue deadline, and receive the requested duration only when allocated. The queue
  is volatile in this simulator. `wait_for_capacity=false` requests immediate shortage.
- Queue order is equal-priority oldest-fit by default. Trusted admin rules can set
  project/BU priority and separate preemption/idle exemptions, shown on reservations.
  The authenticated admin policy API and actual KAI enforcement remain integration gates.
- Unknown dispatch, cancellation, expiry and missing metrics never imply freed capacity.
- The production toggle is designed as global enablement + app/identity policy + request
  opt-in. This initial runtime refuses production even if a flag is changed.
- Python is selected for this I/O-oriented first release and SkyPilot SDK compatibility.
  Rust is a profiling-driven optimization option, not an assumed correctness shortcut.
- The enterprise API target is stateless behind existing GTM, with durable queue,
  idempotency and ledger in PostgreSQL. DB writer fencing, readiness and surviving
  workload reconciliation remain required for safe DR; the simulator is not that target.

## Parent and child modules

| Child of `grace` | Responsibility |
|---|---|
| `domain` | Immutable models, policies, fitting, lifecycle safety |
| `transport` | REST + generated gRPC contract, shared service facade |
| `adapters` | Cluster certification and guarded SkyPilot/KAI compiler |
| `migrations` | Durable PostgreSQL target and transaction functions |
| `deploy` | Parent Helm + control/state/execution child charts |
| `docs` | Architecture, ER, DR, delivery backlog and evidence |
| `tests` | Domain, adapters, actual network transports and adversarial checks |

The Helm execution child is disabled because it has no durable worker yet. Optional
PostgreSQL is a local development dependency only; it is not automatically wired into
the simulation and is not HA. One replica and `Recreate` avoid two independent simulators
briefly appearing to be one reservation authority.

## Run and test locally (no GPU required)

Python 3.12 or newer. From the cloned repository root:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[build,verify]'
python -m grpc_tools.protoc -I proto --python_out=src --grpc_python_out=src proto/grace/v1/grace.proto
PYTHONPATH=src python -m unittest discover -s tests -v
```

The repository includes `uv.lock`, allowing `uv sync --extra build
--extra verify --frozen` for resolved dependency versions. Generated protobuf bindings
are committed; no code generator is needed to run the installed package.

Set a unique demo-only secret, then start:

```bash
export GRACE_DEMO_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m grace.transport.server
```

Windows PowerShell 7:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[build,verify]"
$env:GRACE_DEMO_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
python -m grace.transport.server
```

Listeners default to `127.0.0.1:8080` REST and `127.0.0.1:50051` gRPC. Both have
demo bearer authentication; health endpoints expose only simulation capability status.
The service is not exposed publicly. No Okta credentials or Kubernetes kubeconfig needed.

Create a reservation using PowerShell in another terminal after securely transferring
the demo token to that process:

```powershell
$headers = @{ Authorization = "Bearer $env:GRACE_DEMO_TOKEN"; "Idempotency-Key" = "demo-reservation-1" }
$body = @{
  tenant_id = "demo"; application_id = "demo-app"; environment = "dev"
  gpu_type = "A100-40GB"; gpu_millicards = 250; gpu_memory_mib = 5120
  device_count = 1; duration_seconds = 3600
  data_locations = @("onprem", "gcp-us-central1")
} | ConvertTo-Json
$reservation = Invoke-RestMethod http://127.0.0.1:8080/v1/reservations -Method Post -Headers $headers -ContentType application/json -Body $body
$cancelHeaders = @{ Authorization = "Bearer $env:GRACE_DEMO_TOKEN"; "Idempotency-Key" = "cancel-1"; "If-Match" = $reservation.etag }
Invoke-RestMethod "http://127.0.0.1:8080/v1/reservations/$($reservation.id):cancel" -Method Post -Headers $cancelHeaders
```

The response first becomes `releasing`; the synthetic observer then confirms no live
work exists and makes it `cancelled`. In a live estate that confirmation must come from
fenced execution and real infrastructure, never the user.

## Kubernetes source delivery

Build from the repository root using `docker build -f docker/Dockerfile -t grace-control:0.1.0 .`.
For existing Mac Kind clusters, use [the bash test runner](docs/mac-kind.md).
PowerShell helpers remain documented in `deploy/README.md`. Render first; apply
only to an intended dev/QA namespace. The package supplies no live credentials and
does not create/delete clusters. Production/multi-replica/live-execution configurations
are intentionally rejected by chart guards. Build/render/live test results are
recorded separately in verification; source availability is not deployment evidence.

## Delivery-agent continuity

The project-manager and engineering agents in this delivery are session-scoped. Their
durable output is the backlog and evidence, not an unattended service. To resume:

> Read docs/delivery/status.md, backlog.json and verification.md. Work on the
> earliest dependency-ready task; assign bounded non-overlapping modules; do not mark a
> gate verified without a reproducible result. Preserve live/fractional/prod safeguards.

Next critical work: wire the PostgreSQL repository/outbox and test runtime restart recovery,
implement OIDC and admission, then certify the real SkyPilot→KAI
fractional lifecycle. Existing Windows fake-GPU tests cannot certify CUDA sharing.
