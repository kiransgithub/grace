# KAI / HAMi software GPU limits

GRACE supports three explicit fractional-GPU policies at the adapter boundary.
The code and contract tests are implemented; no real CUDA/HAMi deployment has
been certified by this repository. Public reservation API persistence/wiring for
these policies belongs to the durable execution milestone.

| Mode | Scheduling | Runtime control requested | Required evidence |
| --- | --- | --- | --- |
| `kai_accounting` | Integer millicards and memory accounting translated to KAI | No enforcement assertion | Certified SkyPilot/KAI fractional path |
| `hami_memory` | Same accounting | HAMi-core CUDA memory limit through the KAI binder and resource-isolator | Plugin, actual library loading, memory-overrun test, exact image/library identity, protected injection |
| `hami_memory_sm` | Same accounting | Memory limit plus HAMi-core compute-utilization limit | All memory evidence plus compute test on the exact SkyPilot child-process path |

Installing HAMi does not silently change an existing request's policy. Requiring
HAMi never silently falls back to accounting. A cap on GPU utilization is not a
minimum compute allocation or a promise of 25% application throughput. All modes
continue to use the same reservation ledger and no-double-booking rules.

## Verified upstream contract

The KAI **v0.17.0** documentation includes the `hamicore` integration. It requires
`global.gpuSharing=true`, `binder.plugins.hamicore.enabled=true`, and a separate
`kai-resource-isolator` deployment (the tagged guide uses chart `1.0.0-chart`).
KAI's binder computes a memory limit from the actual allocated portion and the
node's `nvidia.com/gpu.memory` label. Its admission plugin makes the value available
as `CUDA_DEVICE_MEMORY_LIMIT` through a ConfigMap reference. The resource-isolator
loads HAMi-core's `libvgpu.so`, which intercepts CUDA allocation calls.
[KAI v0.17 guide](https://github.com/kai-scheduler/KAI-Scheduler/blob/v0.17.0/docs/gpu-sharing/hami/README.md),
[binder implementation](https://github.com/kai-scheduler/KAI-Scheduler/blob/v0.17.0/pkg/binder/plugins/hamicore/hami_core.go),
[admission implementation](https://github.com/kai-scheduler/KAI-Scheduler/blob/v0.17.0/pkg/admission/webhook/v1alpha2/hamicore/hamicore.go).

HAMi-core documents `LD_PRELOAD` library loading, `CUDA_DEVICE_MEMORY_LIMIT` for
memory and `CUDA_DEVICE_SM_LIMIT` for a utilization percentage. The resource-isolator
integration automates library loading through a mounted preload configuration;
environment variables alone do not install or activate the interception library.
KAI v0.17's inspected binder sets the memory variable only. GRACE's optional SM
mode sets the separate SM variable in the SkyPilot job environment after specific
certification of that execution path.
[HAMi-core README](https://github.com/Project-HAMi/HAMi-core/blob/master/README.md),
[resource-isolator design](https://github.com/Project-HAMi/KAI-resource-isolator#design).

Software CUDA interception can enforce allocation and utilization limits for a
supported, correctly configured runtime. It does not create MIG hardware partitions
or guarantee isolation from adversarial native code. Trusted images, admission
controls and trust-domain placement remain necessary. A workload that can replace
its library or alter the environment of a child process may defeat a software cap;
arbitrary user shell commands are not a security boundary.

## What the translator emits

For a 250-millicard `hami_memory_sm` request on a certified 40960-MiB device:

```yaml
# Relevant output only: this is not a standalone launch file.
envs:
  CUDA_DEVICE_SM_LIMIT: "25"
config:
  kubernetes:
    custom_metadata:
      annotations:
        gpu-fraction: "0.25"
        kai-resource-isolator.io/inject: "true"
        grace.ai/fractional-isolation: hami_memory_sm
        grace.ai/effective-gpu-memory-mib: "10240"
```

The adapter emits neither a fractional `nvidia.com/gpu` request nor duplicate
`CUDA_DEVICE_MEMORY_LIMIT` variables. It does not inject `LD_PRELOAD`, hostPath
volumes or guessed library locations; the certified resource-isolator deployment
owns these. The upstream default path is configurable and must be discovered from
the actual installed release. KAI controls the GPU assignment.

The requested `gpu_memory_mib` must fit within the fractional budget. The effective
memory cap for this KAI `gpu-fraction` path is the allocated fraction's device-memory
budget: a request declaring 10000 MiB with 0.25 of 40960 MiB has an effective budget
of 10240 MiB. The ledger must reserve this effective amount. GRACE exposes that
amount rather than claiming an independent exact 10000-MiB hardware partition.
Reconciliation must compare the actual binder-produced limit to the approved budget.

`gpu-memory` is an alternative KAI request, not an additional cap to combine with
`gpu-fraction`. The tagged documentation describes two-decimal fraction conversion
for memory requests, which can change the effective MiB amount. This adapter retains
the fraction path and its certified precision. SM mode additionally requires
millicards divisible by ten so the percentage is an exact integer; it never rounds
0.251 cards silently to a 25% or 26% SM cap.

## Controller-owned evidence and fail-fast behavior

`CapabilityEvidence.hami_runtime` is an optional `HamiRuntimeEvidence`. It contains
an evidence ID, HAMi-core/resource-isolator versions, the tested library SHA-256,
exact digest-pinned workload images, observation time and strict boolean results.
These records are produced by a trusted controller and certification process;
they are never accepted as proof from a reservation caller.

The translator requires:

1. The existing baseline and fractional SkyPilot/KAI certificates.
2. A stable, certified KAI version at least 0.17.0; a version alone is insufficient.
3. Fresh runtime observations within the cluster's freshness budget.
4. The requested image to match a certified image digest.
5. The HAMi binder plugin enabled, library actually loaded, memory enforcement
   verified, and opt-out controls protected.
6. For SM mode, separately verified compute limiting for the exact workload image,
   SkyPilot task environment and child-process behavior.

Missing or stale evidence produces `HAMI_UNCERTIFIED`, `HAMI_RUNTIME_STALE`,
`HAMI_IMAGE_UNCERTIFIED` or `HAMI_SM_UNCERTIFIED` before any remote submit.
Unsupported topology and unrepresentable SM percentages also fail before submit.
This initial path supports one fractional GPU for one workload container. It does
not generalize certification to full-GPU, multi-device or distributed workloads.

The v0.17 source makes the memory ConfigMap key optional and can skip producing
a limit when the node memory label is invalid. Therefore neither a pod reaching
Running nor the presence of the plugin proves enforcement. The production execution
controller must validate the real library and resolved limits before starting user
computation, and treat missing limits as a failed launch. That admission/controller
implementation is a remaining delivery gate, not part of these in-memory contracts.

## Admission and runtime acceptance

The upstream integration supports disabling injection through pod annotation
`kai-resource-isolator.io/inject: "false"` or namespace label
`kai-resource-isolator.io/webhook=ignore`. GRACE-managed HAMi pools must deny both
to workload identities. Protect allocation/fence labels, CUDA limits, preload
configuration, trusted image/library identities and the namespace itself.
Scope any exception for the isolator's trusted library mount narrowly; do not
grant arbitrary hostPath or privileged access to users.

Run these tests on actual NVIDIA CUDA hardware for every certified stack/image:

| Test | Passing evidence |
| --- | --- |
| Library injection | Correct tested `libvgpu.so` loaded in the actual CUDA process |
| Memory overrun | Allocation within the effective budget succeeds; above-budget allocation is rejected |
| Compute cap | Measured utilization behavior follows the configured SM cap within documented measurement tolerance |
| Shared neighbors | Memory/compute contention and cancellation do not terminate another active share |
| Fail closed | Missing library, failed webhook, missing memory label/key and opt-out attempts block user execution |
| SkyPilot/framework compatibility | Real CUDA, Ray/Dask accounting, child processes and notebook subprocesses work without masking required environment |
| Ownership | Expiry/cancel and stale fences cannot leave unaccounted workloads or release live capacity |

The contract unit tests cover task output, capability gates, stale evidence,
strict types, image identity, topology, integer SM conversion and admission
detection. **A Mac Kind cluster with fake GPUs can test the control-plane flow but
cannot certify NVIDIA CUDA memory or SM enforcement.** Do not label a successful
simulation as a HAMi hardware test.
