# Kubernetes delivery: source package, simulation only

This package provides a parent Helm chart with child modules. It has **not been
deployed in this environment**. A temporary official Helm v3.19.0 binary, verified
against its published SHA-256 checksum, passed lint, template rendering and
negative guard tests. Docker, kubectl and PowerShell were unavailable. Rendered
YAML is not proof that pods start; image-build, PowerShell, target Kubernetes
schema and live-cluster tests remain explicit release gates.

| Child module | Delivered | Runtime status |
|---|---|---|
| `grace-control` | Pod, internal Service, restricted SA/security context, probes, NetworkPolicy | Single in-memory REST/gRPC process |
| `grace-postgresql` | Optional dev StatefulSet + PVC + policy | Not connected to API; backup jobs are not implemented |
| `grace-execution` | Reserved child chart + fail-fast enable guard | Adapter library only; no worker pod yet |

There is no production overlay that can succeed. The `production-blocked.yaml`
file is an intentional rejection test. Replica counts other than one,
autoscaling, production enablement and unsupported state backends fail template
rendering. A Recreate update strategy avoids overlapping independent ledgers;
restarts still lose the entire synthetic reservation inventory.

## Build and render

From `grace/`, on a machine with Docker, Helm 3 and PowerShell 7.3 or newer:

```powershell
.\scripts\Build-Image.ps1 -Image grace-control:dev
.\scripts\Deploy-Simulation.ps1 `
  -Context kind-gpu-onprem `
  -Environment dev `
  -Release grace-dev `
  -Namespace grace-dev `
  -ExistingSecret grace-demo-auth
```

The deployment helper defaults to rendering only. It does not switch the active
kubectl context. `kind-gpu-onprem` is an example from the existing demo, not a
cluster created or accessed by this deliverable. Use a non-production cluster you
are authorized to administer; this simulation does not require GPU nodes.

Before `-Apply`, an operator must:

1. Create an isolated namespace and apply the organization's restricted Pod
   Security Admission policy. Prefer separate dev and QA clusters/identities.
2. Store a randomly generated token of at least 32 characters in a Kubernetes
   Secret named `grace-demo-auth`, key `demo-token`, in that namespace. Do not
   commit the value to Git, paste it into logs, or add it to command-line history.
   Use the organization's secret manager, or `kubectl create secret ...
   --from-file=demo-token=<protected-local-file>` with an explicit context.
3. Make the locally built image available to those nodes: an approved private
   registry or a deliberate `kind load docker-image ... --name <exact-kind-name>`
   on a local development machine. Building an image does not perform this step.
4. Verify the CNI enforces NetworkPolicy and kubelet HTTP probes work with it.
   The default allows no pod callers; local port-forward is the intended demo
   access path. Do not expose the API publicly.
5. Review rendered resources, then repeat the helper with `-Apply`.

Example port forwarding, in two terminals:

```powershell
kubectl --context kind-gpu-onprem -n grace-dev port-forward service/grace-dev-control 8080:8080
kubectl --context kind-gpu-onprem -n grace-dev port-forward service/grace-dev-control 50051:50051
```

The API uses a demo bearer token, **not** AD/Okta multi-tenant authentication.
Health endpoints expose simulation/readiness status; REST and gRPC requests
require the token. Transport security is only the localhost tunnel here.

## Required deployment validation

```powershell
helm lint .\deploy\helm\grace --set-string grace-control.existingSecret=render-only-placeholder
helm template test .\deploy\helm\grace --set-string grace-control.existingSecret=render-only-placeholder
# Each following render MUST exit non-zero:
helm template test .\deploy\helm\grace --set-string grace-control.existingSecret=x --set grace-control.replicaCount=2
helm template test .\deploy\helm\grace --set-string grace-control.existingSecret=x --set grace-control.autoscaling.enabled=true
helm template test .\deploy\helm\grace --set-string grace-control.existingSecret=x --set grace-control.stateBackend=postgres
helm template test .\deploy\helm\grace --set-string grace-control.existingSecret=x --set grace-execution.enabled=true
helm template test .\deploy\helm\grace -f .\deploy\environments\production-blocked.yaml
```

CI must also validate rendered objects against the *target cluster's* Kubernetes
schema, scan/sign the OCI image and check the built container as UID 10001 with
a read-only root filesystem. Then execute authenticated REST/gRPC smoke tests
through the pod, restart it and confirm the documented loss of simulation state.
No test may describe that restart as a successful durability test.

Repeatable tests are in `tests/test_platform.py`. Three source checks always run;
five additional offline Helm tests run when Helm is on PATH or `GRACE_HELM_BIN`
names an approved executable. Missing Helm is reported as a skip, not a pass.
The tests never contact a cluster. Example from `grace/`:

```powershell
$env:GRACE_HELM_BIN = 'C:\approved-tools\helm.exe'
python -m unittest discover -s tests -p test_platform.py -v
```

Verification used the official [Helm release archive](https://get.helm.sh/helm-v3.19.0-linux-amd64.tar.gz)
and [published SHA-256 checksum](https://get.helm.sh/helm-v3.19.0-linux-amd64.tar.gz.sha256sum).
This records the tested version, not a claim that it is the latest supported version.

## Future durable deployment

Production needs a transactional PostgreSQL repository, externally managed
HA/PITR PostgreSQL, a durable execution worker/outbox, Okta validation, TLS/mTLS,
real discovery/admission/reconciliation, and a completed disaster-recovery drill.
Only then enable multiple API pods, safe rolling updates, a PDB with
`minAvailable: 2` for three replicas, topology spreading and HPA. Add separate
worker/sweeper/reconciler Deployments with their own least-privilege identities.

The external DB connection will be injected from a managed Secret with verified
TLS. No `DATABASE_URL` value currently changes API storage: failing closed is
preferable to implying durability from an unused configuration setting.

See [operations.md](../docs/operations.md) for identity boundaries, per-state-store
DR ownership, failure procedures and acceptance criteria.
