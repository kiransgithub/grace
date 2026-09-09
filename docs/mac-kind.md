# Test GRACE on your existing Mac Kind clusters

This workflow builds the GRACE image for your existing Kind node architecture, loads it into those nodes, deploys one isolated dev/QA pod, and tests its real REST and gRPC endpoints through a temporary loopback port-forward.

The current runtime uses synthetic GPU inventory and in-memory reservation state. This test proves the container, chart, transports and reservation accounting work together. It does not dispatch a SkyPilot task or exercise the installed KAI scheduler. Fake GPU labels on Mac cannot verify CUDA interception, HAMi memory enforcement, GPU compute throttling or physical GPU performance; those require a supported NVIDIA Linux GPU cluster. Each selected Kind cluster gets an independent simulator; this is not a multi-cluster shared control plane or an HA deployment.

## Prerequisites

- A running Docker engine on the Mac, with access to the same Docker endpoint that owns the existing Kind containers.
- `kind`, `kubectl`, Helm 3, and Python 3.12 or newer on `PATH`.
- Existing ready Kind clusters and their normal `kind-NAME` kubeconfig contexts.
- Network access to fetch the approved Python base image and the dependencies pinned in `pyproject.toml`.
- Permission to create a dedicated namespace, Secret, ConfigMap, Helm release and port-forward on the selected demo cluster.

The helper requires neither host CUDA nor physical GPUs. It uses Linux `arm64` images on Apple Silicon Kind nodes and Linux `amd64` images on Intel nodes. It reads architecture from the Kubernetes nodes, verifies their Docker Kind ownership and refuses mixed or unknown architectures.

From this repository's root:

```bash
python3 --version
docker info
kind get clusters
kubectl config get-contexts
bash scripts/test-kind.sh --list
```

`--list` performs read-only discovery. It does not build images, install dependencies, create namespaces or change the current context. A renamed Kubernetes context is intentionally excluded; use the existing normal `kind-NAME` context associated with that cluster. The helper will not rename contexts for you.

If your Python 3.12 executable has a separate name:

```bash
export GRACE_PYTHON=python3.12
```

## Deploy and test one selected cluster

Replace the context below with an exact context printed by `--list`:

```bash
bash scripts/test-kind.sh --context kind-gpu-onprem
```

For QA:

```bash
bash scripts/test-kind.sh --context kind-gpu-onprem --environment qa
```

To explicitly test every discovered Kind context, sequentially:

```bash
bash scripts/test-kind.sh --all-kind
```

`--all-kind` validates every target's node ownership and architecture before proceeding. If one target is production-named, unhealthy or mismatched, the helper refuses the run. Select an eligible context individually to proceed. Failures after deployments begin stop the sequence and retain completed deployments for inspection.

For each target, the workflow:

1. Verifies the exact context, Kubernetes node names, Docker Kind labels and node readiness.
2. Installs the project/client dependencies into `.venv/kind-client`, builds one image per required architecture and validates the chart with Helm lint/template.
3. Loads the image into the selected existing Kind cluster. It never creates, resets or deletes a cluster.
4. Creates or verifies the owned namespace, acquires a per-namespace operation lock and creates/reuses a private demo-token Secret.
5. Checks any existing GRACE release's identity. If the service already exists, confirms that it has no active reservations before upgrading the volatile simulator.
6. Deploys one pod, waits for readiness, opens temporary loopback REST/gRPC forwarding and runs the API smoke test.
7. Stops forwarding and removes the temporary credential file. The deployed pod remains available for inspection.

| Environment | Namespace | Helm release | Service |
|---|---|---|---|
| dev | `grace-kind-dev` | `grace-dev` | `grace-dev-control` |
| qa | `grace-kind-qa` | `grace-qa` | `grace-qa-control` |

The namespace and Secret must carry `grace.dev/owner=grace-kind-demo` and the matching `grace.dev/environment` label. A pre-existing unowned namespace or Secret is never adopted. Credentials are generated randomly, carried via standard input to Kubernetes, and placed in a temporary mode-0600 file for the smoke client. Tokens are never command-line arguments or normal output.

The operation lock prevents simultaneous helper runs in the same namespace. The active-reservation preflight is a point-in-time check; do not run other API clients concurrently with this test or upgrade. The simulation does not implement a durable admission-freeze transaction. An idle upgrade also loses the simulator's terminal reservation history.

## What a successful test proves

The test uses one identity for both transports and one synthetic `onprem` A100 device:

- Missing credentials are rejected by REST and gRPC.
- A reservation created over REST can be retrieved over gRPC.
- Replaying the same create request preserves the reservation ID and allocation.
- After the first 250/1000 share is allocated, eight concurrent 250/1000 requests admit exactly three additional reservations. Excess requests receive capacity errors.
- Cancellation submitted and replayed over gRPC has a stable result.
- The synthetic observer confirms cleanup before the test asks for reuse.
- A full-device request succeeds after all four fractional reservations have been released.
- The test cancels the full-device request and confirms cleanup.

Expected final output contains:

```text
PASS: authenticated REST + gRPC; shared state; create/cancel replay; concurrent fractional no-overbooking; confirmed cleanup; full-device reuse.
Scope: synthetic accounting in a Kubernetes pod; no SkyPilot, CUDA or HAMi runtime test.
PASS kind-gpu-onprem. The simulation pod remains; temporary port-forward and token file removed.
```

The selected Kubernetes context determines where the GRACE pod runs. The request's synthetic `onprem` location does not select another real Kubernetes context. Real infrastructure onboarding, persistent state and dispatch remain separate integration milestones.

## Reuse a previously built local image

The helper defaults to a unique tag per build to avoid silently running an old image. CI or an operator may explicitly supply a previously built local image:

```bash
bash scripts/test-kind.sh --context kind-gpu-onprem \
  --skip-build --image grace-control:ci
```

The helper verifies that the image architecture matches the selected nodes and still loads the image into Kind. The current image argument supports a simple tagged local name, including repository paths; registry names with explicit port numbers are rejected. First-run dependency/image downloads can take several minutes. Build, install and image-load operations have 15-minute deadlines; Helm readiness has a 3-minute deadline and port-forward setup has a 30-second deadline.

## Inspect or clean up explicitly

Use the same exact context and namespace:

```bash
kubectl --context kind-gpu-onprem -n grace-kind-dev get pods,services
kubectl --context kind-gpu-onprem -n grace-kind-dev logs deployment/grace-dev-control --tail=100
kubectl --context kind-gpu-onprem -n grace-kind-dev get events --sort-by=.metadata.creationTimestamp
```

Failures preserve deployed resources. The helper does not attempt a rollback or cluster reset. If the current deployment has active reservations, cancel them through its API or allow them to expire; it will not clear them to make the test pass.

When you explicitly want to remove this demo release, including its volatile state:

```bash
bash scripts/test-kind.sh --context kind-gpu-onprem --cleanup
```

For the QA release, add `--environment qa`. Cleanup requires matching namespace/Secret ownership and the GRACE Helm chart identity. It removes only the named release and its owned auth Secret, retaining the namespace, other releases, existing KAI components, nodes and clusters. An explicit cleanup can remove active simulator reservations, so use it only when finished with that demo.

If a process was forcibly killed and an operation lock remains, first confirm that no helper is still running and inspect the exact lock:

```bash
kubectl --context kind-gpu-onprem -n grace-kind-dev get configmap grace-kind-operation -o yaml
```

After confirming it is stale, an operator can explicitly remove that single ConfigMap and rerun the helper. There is no automatic lock takeover.

## Verification boundary for this revision

The helper has offline argument, ownership, architecture, timeout and credential-handling tests. Its exact smoke client has also passed against real local REST/gRPC sockets and the synthetic lifecycle observer. Those checks do not establish that this revision ran on your Mac or its Kind clusters. A successful invocation on that host is the required Kubernetes deployment evidence; preserve the command output with the tested Git commit.
