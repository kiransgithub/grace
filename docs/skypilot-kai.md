# SkyPilot / KAI execution and onboarding contract

Status: dependency-free translator, onboarding reference and fault tests implemented.
Real SkyPilot dispatch, real GPU isolation and a production admission controller
have **not** been validated by this package. Live dispatch defaults OFF. No
Kubernetes create fallback exists. This is the standalone GRACE repository.

## Responsibility boundary

| Component | Owns | Must not assume |
| --- | --- | --- |
| GRACE reservation service | Entitlement, atomic budget, expiry, placement and fencing | A GPU is healthy because the DB says so |
| Durable dispatch worker | Outbox claim, single owner, operation correlation and recovery | A deterministic name makes remote writes exactly-once |
| This adapter | Validated translation, exact approved context, conservative error mapping | That a launch receipt means a running workload |
| SkyPilot execution realm | Workload bootstrap and lifecycle on existing Kubernetes | Permission to choose a different environment or create cloud VMs |
| KAI and HAMi runtime | Scheduling, device visibility and certified CUDA memory/compute limits | Software interception is MIG hardware isolation or a throughput SLA |
| Trusted reconciler | Actual pods, GPU assignments, sharing accounting and release evidence | A missing SkyPilot dashboard entry means GPUs are free |

The runnable Python seam is appropriate here because SkyPilot exposes a Python
SDK. It is not a reason to run SDK calls inside public HTTP/gRPC handlers. A Rust
control-plane module can invoke the execution worker through an internal versioned
protocol, while the worker retains the Python SDK dependency. Network and GPU
provisioning latency, not task-dictionary construction, dominates this boundary.

## Integration callable

```python
from grace.adapters import build_task, SkyPilotAdapter

# spec: frozen, validated DispatchSpec constructed from the committed reservation
# cluster: trusted, certified ClusterRegistration loaded from inventory
task = build_task(spec, cluster, now=clock.now())

# Only a durable worker uses the optional live seam:
adapter = SkyPilotAdapter(
    live_enabled=operator_live_gate,
    allow_experimental_fractional=operator_fractional_gate,
    authorize_lease=transactional_lease_check,
)
receipt = adapter.submit(spec, cluster, now=clock.now())
# Persist operation_id; asynchronously reconcile; do not re-launch on timeout.
```

`DispatchSpec` has `reservation_id`, `allocation_id`, `dr_epoch`, `fencing_token`,
`environment`, `pool_id`, `gpu_model`, `gpu_millicards`, `gpu_memory_mib`,
`device_count`, `queue`, `image`, `run`, `cpu_count`, `memory_gib`,
`production_authorized`, `trusted_tenant_id` and `fractional_isolation`. CPU and runtime RAM are host
resources, separate from accelerator frame-buffer memory.

`dr_epoch` is a positive integer recovery generation, separate from the per-owner
`fencing_token`. The DR epoch's compatibility default is 1 for local fixtures,
but a live worker must populate it from the
authoritative DB. `LeaseAuthorization` is the required return from
`transactional_lease_check`: it contains `reservation_id`, `allocation_id`,
`dr_epoch`, `fencing_token` and a strict boolean `authorized`. The adapter requires
all four identity fields to match the queued spec exactly; a bare boolean is
rejected. Admission must compare the same pair from `grace.ai/dr-epoch` and
`grace.ai/fencing-token` against current authoritative lease state.

`gpu_millicards` is an integer from 1 to 1000 **per device**, and `device_count` is
the number of devices for one pod/node. Full-GPU requests set millicards to 1000.
Distributed multi-node and fractional multi-device translation need a separate
capability and gang-accounting contract; this first translator rejects unsupported
fractional multi-device requests. It does not silently inflate fractional requests
into full cards. Exact GPU IDs remain scheduler observations, not SkyPilot task
selector values.

## Fractional GPU compatibility gate

KAI uses `gpu-fraction` or `gpu-memory` annotations for scheduling. **KAI >=0.17
with its `hamicore` plugin and the separately deployed `kai-resource-isolator`
can enforce CUDA memory limits through HAMi-core.** HAMi-core also supports
compute-utilization limiting with `CUDA_DEVICE_SM_LIMIT`; KAI v0.17's binder
automatically sets the memory variable, not the SM variable. This corrects the
earlier design's omission of HAMi enforcement. The resulting limits are software
controls, not MIG hardware partitions or proportional application throughput
guarantees. [Pinned KAI v0.17 integration](https://github.com/kai-scheduler/KAI-Scheduler/blob/v0.17.0/docs/gpu-sharing/hami/README.md),
[HAMi-core controls](https://github.com/Project-HAMi/HAMi-core/blob/master/README.md).

`FractionalIsolation` distinguishes `kai_accounting`, `hami_memory` and
`hami_memory_sm`. Accounting remains the compatibility default. Selecting a HAMi
mode requires fresh controller-owned `HamiRuntimeEvidence`: exact approved workload
image digests and library hash, plugin enablement, observed library loading,
verified memory behavior and protected admission. SM mode requires separate compute
test evidence and exact integer percentage conversion. It does not silently downgrade
to accounting if any evidence is absent. [Complete HAMi contract](hami.md).

The official SkyPilot issue illustrates that Kubernetes rejects fractional
`nvidia.com/gpu` quantities. The experimental translator does **not** set
`accelerators: A100:0.25` or `nvidia.com/gpu: 0.25`.
[SkyPilot fractional Kubernetes issue](https://github.com/skypilot-org/skypilot/issues/2655).

For a certified 250-millicard request, it instead emits an exact-context **CPU
SkyPilot task**, injects `gpu-fraction: "0.25"`, chooses the model/pool/trust node
labels, and uses KAI. Device exposure is left to the certified KAI/runtime path;
the adapter never sets `NVIDIA_VISIBLE_DEVICES=all`. It emits GRACE's memory budget
annotation, not a second conflicting `gpu-memory` request. For HAMi, it requests
isolator injection and reports the effective fractional memory budget. KAI owns
`CUDA_DEVICE_MEMORY_LIMIT`; the isolator owns `libvgpu.so` injection. GRACE does not
invent library mounts or duplicate that memory variable. Certified SM mode emits
`CUDA_DEVICE_SM_LIMIT` into the SkyPilot job environment.

This is an integration hypothesis, **not a claim of native SkyPilot fractional
support**. SkyPilot/Ray may register zero GPU resources for a CPU task even if the
container can execute CUDA. CUDA visibility, Ray GPU resource accounting, environment
variable masking, child processes, Dask workers and framework scheduling must all
pass on the exact version pair. If a workload requires Ray's GPU scheduler and
it reports zero GPUs, certification must fail. There is no automatic bare-pod
fallback. The milestone stays blocked until the approved SkyPilot path works.

The source review verified the tagged KAI v0.17.0 HAMi guide and the binder/admission
implementation; it does not establish a passing hardware result for SkyPilot
0.13.0 / KAI 0.17.0 in this deployment. Keep version strings and container digests in the certificate; changing
the SDK, server, scheduler, device plugin or node runtime invalidates it. The
SDK seam checks its version against the certificate before launch.

The contract accepts integer millicards, while the cluster model declares its
tested fraction quantum. Its conservative example quantum is 10 (one hundredth
of a card), not a universal assertion about KAI precision. A 1-millicard request
is rejected unless that exact precision has been certified; never round up or
down without returning the effective allocation to the user and ledger.

### Required hardware certification

1. Launch a full-card task through GRACE -> SkyPilot -> KAI. Verify exact context,
   model, pod identity, queue, fencing token, GPU UUID and successful CUDA work.
2. Launch two 0.5-card tasks. Verify both device identities and aggregate accounting;
   then request a third task and prove it waits or selects another eligible device.
3. Check per-device frame-buffer accounting with 40 GB and 80 GB models separately.
   Account for driver/runtime overhead and application peaks; do not use decimal
   marketing GB as measured allocatable MiB.
4. Exercise real memory over-consumption, compute contention and framework GPU
   discovery. HAMi mode requires the selected software limits to work in the actual
   CUDA process, including subprocesses. Use reviewed same-trust-domain dev/QA
   pools: software limits do not create a hostile-tenant hardware security boundary.
5. Cancel one shared workload. Prove that its share becomes reusable without
   killing another tenant's shared-device workload or KAI reservation pod.
6. Exercise Ray and Dask tasks that request GPUs, not only `nvidia-smi` or sleep.
   Test notebook subprocesses and reconnect behavior.
7. Attempt bypass with CPU-only resource declarations and KAI annotations. Verify
   unauthorized requests, stale fences and resource-amending patches are rejected.
8. Kill the worker after SkyPilot accepts a launch but before its receipt is stored.
   Reconcile without launching a replacement; verify a single surviving owner.

## Seamless onboarding without capacity double counting

The reference `ClusterRegistry.register()` accepts an idempotency key. Identical
retries return the same registration; key/body mismatches fail. Duplicate physical
Kubernetes UIDs or context aliases fail, preventing one cluster from becoming two
independent inventory supplies. This reference registry is intentionally in-memory;
production persistence and multiple pools per cluster belong to the normalized DB
cluster/pool model, not duplicated registrations.

Transition sequence: `registered -> discovered -> validating -> certified ->
enabled -> draining -> disabled`. Disabled clusters must rediscover and recertify
before enabling. Discovery records cluster UID, environment, control endpoint,
credential **reference**, pool membership, GPU models and allocatable MiB. Never
accept inline tokens or kubeconfig bytes from a reservation request.

Validation covers private connectivity, read/list/watch, dispatcher impersonation
boundaries, queue admission, health, runtime, CUDA, telemetry, price metadata,
dataset attestations, failure domains and rollback. Certification attaches an
evidence ID and exact versions. GPU observations have timestamps and a freshness
budget; stale and future-skewed inventory fails closed. Draining blocks new
dispatch but allows observation/termination; disabling requires zero active leases.

The output binds one exact `k8s/<context>` and namespace. Do not provide alternate
SkyPilot resources or new cloud identities to the SDK. Cross-location placement is
GRACE policy, within the same authorized environment/data boundary. `dev`, `qa`
and `prod` are not interchangeable geographic locations. Production requires both
an operator-enabled pool and an authenticated, policy-approved request; the request
flag is not a user privilege grant.

SkyPilot documents Kubernetes pod customization and context-specific configuration.
Those settings implement routing, not authorization. Keep all kubecontexts and
credentials on the execution realm, and regenerate its approved configuration only
from certified inventory. [SkyPilot advanced configuration](https://docs.skypilot.ai/en/latest/reference/config.html).

## Fail-fast and ambiguous-outcome handling

| Event | Result | Required next step |
| --- | --- | --- |
| Invalid amount/model/pool/queue; stale inventory; disabled gate | REJECTED before remote call | Fix request or cluster readiness |
| SDK/task parse failure before launch | REJECTED | Inspect sanitized internal diagnostic |
| SDK returns request ID | ACCEPTED, not RUNNING | Persist ID; observe actual infrastructure |
| Any exception once launch/down may have begun | UNKNOWN | Retain capacity; reconcile; do not spill |
| SDK returns `None`, `"None"` or unexpected receipt type | UNKNOWN | Same conservative reconciliation |
| `sky.down` accepted | ACCEPTED, not RELEASED | Wait for authoritative release evidence |
| Dispatch reconciled, ownership fenced, termination requested, fresh successful inventory read, workloads absent and scheduler share released | RELEASE_CONFIRMED | Atomic ledger release and new placement evaluation |

The dispatch worker must obtain an outbox lease and compare the stored canonical
task hash before invoking this adapter. A transactionally current fence is necessary
but not sufficient: admission must enforce it downstream. A process paused between
lease validation and remote submission must not regain write authority after failover.
Retain enough operation/owner history to reconcile delayed writes. Disable automatic
SkyPilot retries (`retry_until_up=False`) and autonomous replacement until GRACE's
same-reservation deadline, budget and single-owner recovery contract is implemented.

On DR promotion, fence the former writer and execution authority, bump `dr_epoch`,
then reconcile before dispatch resumes. Deterministic SkyPilot names include the
original execution epoch so an old generation cannot alias a new one. Persist the
original immutable dispatch identity and actual remote name: do not recompute an
old workload's teardown name using a new DR epoch. Cross-epoch cleanup/adoption is
an explicit reconciliation operation, not permission to replace a possibly running
workload automatically. Release evidence must match the spec's DR epoch as well
as its allocation, fence token and physical cluster UID.

On cancellation or expiry, revoke future launch authority first, queue termination,
then observe. A single momentary absence observation is insufficient while an
unfenced old request can still create a pod. `AbsenceEvidence.ownership_fenced`
means the trusted reconciler has proved this downstream guarantee, not merely that
a database lease expired. Evidence must also contain `fenced_at` and
`stop_requested_at` timestamps, with `observed_at > max(fenced_at, stop_requested_at)`.
The STOP timestamp records intent and can precede or follow fencing; neither is
proof of termination. `dispatch_reconciled` must explicitly be true after delayed
launch outcomes are resolved (it defaults false). Old absence observations cannot
release a new allocation. KAI
reservation pods can remain for other legitimate
shares; confirm the allocation's share release rather than deleting the shared pod.

## Anti-bypass deployment requirements

`requires_gpu_admission()` is a tested **detection contract**, not a deployed
admission webhook. Admission must cover pod CREATE/UPDATE and workload-controller
templates, full `nvidia.com/*` resources, KAI fractional/memory/container annotations,
GPU runtime classes/selectors and privileged device access. Enforce protected GPU
nodes, taints and restrictive workload permissions even for pods labelled CPU-only.
Check actual authenticated dispatcher identity, namespace, immutable allocation
budget, active fence and tenant. User-supplied reservation labels alone prove nothing.

Use Pod Security restrictions to deny privileged mode, host PID, host devices,
unsafe hostPath mounts and escalation; deny service-account impersonation and
namespace/queue label changes. KAI system reservation pods require a narrow
controller-identity exception, not a broad namespace exemption. If live lease
validation is unavailable, deny new GPU admission while already-authorized workloads
continue under their bounded leases. These are deployment acceptance gates, not
properties conferred by this Python module.

For HAMi pools, deny `kai-resource-isolator.io/inject: "false"`, the namespace
label `kai-resource-isolator.io/webhook=ignore`, and unapproved preload/library or
CUDA-limit changes. The upstream isolator uses trusted library mounts that may
need a narrowly scoped Pod Security exception; do not allow arbitrary user
hostPath mounts. The adapter's admission detection now also covers isolator
annotations and CUDA memory/SM variables. It still is not a deployed webhook.

## Local verification

```sh
PYTHONPATH=src python -m unittest discover -s tests -p 'test_adapter*.py' -v
```

Tests cover translation, gate failures, identity conflicts, stale inventory,
production authorization, fractional memory/precision/topology checks, SDK fault
mapping, no blind retries, cancellation receipts and release evidence. Fake SDK
tests exercise this adapter only; they do not certify Kubernetes, real GPUs, DCGM,
Okta, SkyPilot HA, distributed gang scheduling or DR. Kind on a Mac can validate
API and Kubernetes control flow; simulated GPU resources cannot certify CUDA or HAMi.
