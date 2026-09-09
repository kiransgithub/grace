#!/usr/bin/env python3
"""Build, deploy and test the isolated simulator on explicitly selected Kind."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
OWNER = "grace-kind-demo"
OWNER_KEY = "grace.dev/owner"
PRODUCTION = re.compile(r"(?:^|[-_.])(prod|production|platinum)(?:$|[-_.])", re.I)


class DemoError(RuntimeError):
    pass


def command(args, *, stdin=None, allow_failure=False, sensitive=False, timeout=90):
    try:
        result = subprocess.run(args, input=stdin, text=True, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DemoError(f"{args[0]} {args[1]} exceeded its {timeout}-second deadline") from None
    if result.returncode and not allow_failure:
        # Do not echo command input: Kubernetes Secret manifests contain credentials.
        detail = "credential operation failed; inspect Secret metadata" if sensitive else result.stderr.strip()[:1500]
        raise DemoError(f"{args[0]} {args[1]} failed: {detail}")
    return result


def required(*names):
    for name in names:
        if not shutil.which(name):
            raise DemoError(f"Missing prerequisite: {name}. See docs/mac-kind.md.")


def discover():
    required("kind", "kubectl")
    clusters = command(["kind", "get", "clusters"]).stdout.splitlines()
    contexts = set(command(["kubectl", "config", "get-contexts", "-o", "name"]).stdout.splitlines())
    return {f"kind-{name}": name for name in clusters if f"kind-{name}" in contexts}


def select_targets(args, available):
    if args.context:
        if args.context not in available:
            raise DemoError("Context is not an existing, exact Kind context. No deployment was attempted.")
        selected = {args.context: available[args.context]}
    else:
        selected = dict(available)
    if not selected:
        raise DemoError("No matching existing Kind clusters and contexts were found.")
    for context in selected:
        if PRODUCTION.search(context):
            raise DemoError(f"Production-named context refused: {context}")
    return selected


def kube(context, *args, **kwargs):
    return command(["kubectl", "--context", context, "--request-timeout=30s", *args], **kwargs)


def inspect_target(context, cluster):
    """Match node names and Kind Docker labels before any mutation."""
    expected = set(command(["kind", "get", "nodes", "--name", cluster]).stdout.splitlines())
    nodes = json.loads(kube(context, "get", "nodes", "-o", "json").stdout)["items"]
    if not expected or {node["metadata"]["name"] for node in nodes} != expected:
        raise DemoError(f"{context}: API node names do not match the selected Kind cluster.")
    arches = set()
    for node in nodes:
        name = node["metadata"]["name"]
        docker_cluster = command(["docker", "inspect", "--format",
            '{{ index .Config.Labels "io.x-k8s.kind.cluster" }}', name]).stdout.strip()
        if docker_cluster != cluster:
            raise DemoError(f"{name}: Docker Kind ownership does not match {cluster}.")
        info = node["status"]["nodeInfo"]
        if info.get("operatingSystem") != "linux":
            raise DemoError("Only Linux Kind nodes are supported.")
        arches.add(info.get("architecture"))
        if not any(c.get("type") == "Ready" and c.get("status") == "True"
                   for c in node["status"].get("conditions", [])):
            raise DemoError(f"{name}: node is not Ready; repair the existing cluster first.")
    if len(arches) != 1 or not arches <= {"amd64", "arm64"}:
        raise DemoError("Mixed or unsupported node architecture; no image was built.")
    return arches.pop()


def owned(resource, environment):
    labels = resource.get("metadata", {}).get("labels", {})
    return labels.get(OWNER_KEY) == OWNER and labels.get("grace.dev/environment") == environment


def get_resource(context, kind, name, namespace=None):
    flags = ["-n", namespace] if namespace else []
    result = kube(context, *flags, "get", kind, name, "--ignore-not-found", "-o", "json")
    return json.loads(result.stdout) if result.stdout.strip() else None


def check_namespace(context, namespace, environment):
    existing = get_resource(context, "namespace", namespace)
    if existing is not None and not owned(existing, environment):
        raise DemoError(f"{namespace}: existing namespace is not owned by this helper; refusing changes.")
    return existing


def labels(environment):
    return {OWNER_KEY: OWNER, "grace.dev/environment": environment}


def ensure_namespace(context, namespace, environment):
    if check_namespace(context, namespace, environment) is None:
        payload = {"apiVersion": "v1", "kind": "Namespace", "metadata": {
            "name": namespace, "labels": labels(environment)}}
        # Create (not apply) so an ownership race fails instead of adopting a namespace.
        kube(context, "create", "-f", "-", stdin=json.dumps(payload))


def ensure_token(context, namespace, environment, token_path):
    name = "grace-demo-auth"
    existing = get_resource(context, "secret", name, namespace)
    if existing is not None:
        if not owned(existing, environment):
            raise DemoError("Existing demo Secret is not owned by this helper.")
        try:
            token = base64.b64decode(existing["data"]["demo-token"], validate=True).decode("ascii")
        except (KeyError, ValueError, UnicodeError) as exc:
            raise DemoError("Owned demo Secret is malformed; repair it explicitly.") from exc
        if len(token) < 32 or any(c.isspace() for c in token):
            raise DemoError("Owned demo Secret must contain at least 32 non-whitespace characters.")
    else:
        token = secrets.token_urlsafe(48)
        payload = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": name, "namespace": namespace, "labels": labels(environment)},
            "data": {"demo-token": base64.b64encode(token.encode()).decode()}}
        kube(context, "create", "-f", "-", stdin=json.dumps(payload), sensitive=True)
    token_path.write_text(token)
    token_path.chmod(0o600)
    return name


def install_client():
    if sys.version_info < (3, 12):
        raise DemoError("Python 3.12+ is required. Set GRACE_PYTHON=python3.12 if needed.")
    venv = ROOT / ".venv" / "kind-client"
    python = venv / "bin" / "python"
    if not python.exists():
        command([sys.executable, "-m", "venv", str(venv)])
    print("Installing the pinned GRACE client dependencies into .venv/kind-client.", flush=True)
    command([str(python), "-m", "pip", "install", "--disable-pip-version-check", str(ROOT)], timeout=900)
    return python


@contextmanager
def operation_lock(context, namespace, environment):
    """Do not let two invocations reset/test the same volatile authority."""
    payload = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {
        "name": "grace-kind-operation", "namespace": namespace, "labels": labels(environment)},
        "data": {"operation_id": secrets.token_hex(16)}}
    result = kube(context, "create", "-f", "-", stdin=json.dumps(payload), allow_failure=True)
    if result.returncode:
        raise DemoError("Cannot acquire operation lock. Another helper may be running; "
                        "inspect configmap/grace-kind-operation before explicitly removing a stale lock.")
    try:
        yield
    finally:
        kube(context, "-n", namespace, "delete", "configmap", "grace-kind-operation",
             "--wait=true", allow_failure=True)


def cleanup(context, namespace, release, environment):
    existing = check_namespace(context, namespace, environment)
    if existing is None:
        print(f"{context}: no owned namespace; nothing to clean up.")
        return
    check_release(context, namespace, release)
    secret = get_resource(context, "secret", "grace-demo-auth", namespace)
    if secret is not None and not owned(secret, environment):
        raise DemoError("Cleanup refused: Secret ownership differs.")
    result = command(["helm", "uninstall", release, "--kube-context", context,
                      "--namespace", namespace, "--ignore-not-found", "--wait", "--timeout", "3m"], timeout=210)
    if result.stdout:
        print(result.stdout.strip())
    if secret is not None:
        kube(context, "-n", namespace, "delete", "secret", "grace-demo-auth", "--wait=true")
    print(f"{context}: removed the owned release and auth Secret; retained the namespace and cluster.")


def check_release(context, namespace, release):
    releases = json.loads(command(["helm", "list", "--kube-context", context, "--namespace", namespace,
                                   "--filter", f"^{release}$", "--all", "-o", "json"]).stdout)
    if any(item.get("name") != release or item.get("namespace") != namespace
           or not re.fullmatch(r"grace-[0-9].*", item.get("chart", "")) for item in releases):
        raise DemoError("Existing Helm release does not identify the GRACE parent chart; refusing changes.")
    deployment = get_resource(context, "deployment", f"{release}-control", namespace)
    if deployment is not None:
        metadata = deployment.get("metadata", {})
        if metadata.get("labels", {}).get("app.kubernetes.io/part-of") != "grace" or \
           metadata.get("annotations", {}).get("meta.helm.sh/release-name") != release:
            raise DemoError("Existing deployment is not the owned GRACE Helm release.")
    return deployment


def smoke(context, namespace, release, python, token_path, environment, *, idle_only=False):
    # Let kubectl bind unused loopback ports; avoid fixed-port collisions across clusters.
    with tempfile.TemporaryFile(mode="w+") as output:
        process = subprocess.Popen(["kubectl", "--context", context, "--namespace", namespace,
            "port-forward", f"service/{release}-control", ":8080", ":50051", "--address=127.0.0.1"],
            stdout=output, stderr=subprocess.STDOUT, text=True)
        try:
            ports = {}
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise DemoError("Port-forward stopped before both API listeners were available.")
                output.seek(0)
                for local, remote in re.findall(r"Forwarding from 127\.0\.0\.1:(\d+) -> (\d+)", output.read()):
                    ports[int(remote)] = int(local)
                if {8080, 50051} <= ports.keys():
                    break
                time.sleep(0.1)
            else:
                raise DemoError("Port-forward readiness timed out after 30 seconds.")
            result = command([str(python), str(ROOT / "scripts" / "smoke-api.py"),
                "--http", f"http://127.0.0.1:{ports[8080]}",
                "--grpc", f"127.0.0.1:{ports[50051]}",
                "--token-file", str(token_path), "--environment", environment,
                *(["--assert-idle-only"] if idle_only else [])])
            print(result.stdout.strip())
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--list", action="store_true", help="Read-only Kind/context discovery")
    target.add_argument("--context", help="One exact existing kind-NAME context")
    target.add_argument("--all-kind", action="store_true", help="Explicitly deploy/test each discovered Kind cluster")
    parser.add_argument("--environment", choices=("dev", "qa"), default="dev")
    parser.add_argument("--cleanup", action="store_true", help="Remove only the helper-owned release and Secret")
    parser.add_argument("--skip-build", action="store_true", help="Use an existing local image supplied by --image")
    parser.add_argument("--image", help="Existing tagged local image, used only with --skip-build")
    result = parser.parse_args(argv)
    if result.list and result.cleanup:
        parser.error("--list cannot be combined with --cleanup")
    if bool(result.image) != result.skip_build:
        parser.error("--skip-build and --image must be supplied together")
    if result.image and not re.fullmatch(r"[a-z0-9][a-z0-9./_-]*:[A-Za-z0-9][A-Za-z0-9_.-]*", result.image):
        parser.error("--image must be a simple tagged local image (registry ports are unsupported)")
    return result


def main(argv=None):
    args = arguments(argv)
    available = discover()
    if args.list:
        print("CONTEXT\tKIND CLUSTER\tELIGIBILITY")
        for context, cluster in sorted(available.items()):
            print(f"{context}\t{cluster}\t{'REFUSED: production name' if PRODUCTION.search(context) else 'dev/qa only'}")
        if not available:
            print("No exact Kind contexts found. This helper will not create or rename clusters.")
        return
    targets = select_targets(args, available)
    required("docker", "helm")
    namespace, release = f"grace-kind-{args.environment}", f"grace-{args.environment}"
    # Validate every target before building or applying anything on --all-kind.
    arches = {context: inspect_target(context, cluster) for context, cluster in targets.items()}
    for context in targets:
        check_namespace(context, namespace, args.environment)
    if args.cleanup:
        for context in targets:
            if check_namespace(context, namespace, args.environment) is not None:
                with operation_lock(context, namespace, args.environment):
                    cleanup(context, namespace, release, args.environment)
        return
    python = install_client()
    images = {}
    for arch in sorted(set(arches.values())):
        if args.skip_build:
            actual = command(["docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", args.image]).stdout.strip()
            if actual != f"linux/{arch}":
                raise DemoError(f"Local image architecture {actual} does not match linux/{arch}.")
            images[arch] = args.image
            continue
        image = f"grace-control:kind-{arch}-{secrets.token_hex(4)}"
        print(f"Building {image} for existing linux/{arch} nodes.", flush=True)
        command(["docker", "build", "--platform", f"linux/{arch}", "--file",
                 str(ROOT / "docker" / "Dockerfile"), "--tag", image, str(ROOT)], timeout=900)
        images[arch] = image
    for context, cluster in targets.items():
        image = images[arches[context]]
        print(f"Testing {context}, namespace {namespace}: synthetic GPUs; no CUDA dispatch.", flush=True)
        overrides = ["--values", str(ROOT / "deploy" / "environments" / f"{args.environment}.yaml"),
            "--set-string", "grace-control.existingSecret=grace-demo-auth",
            "--set-string", f"grace-control.image.repository={image.split(':')[0]}",
            "--set-string", f"grace-control.image.tag={image.split(':')[1]}",
            "--set-string", "grace-control.image.pullPolicy=Never"]
        chart = str(ROOT / "deploy" / "helm" / "grace")
        command(["helm", "lint", chart, *overrides])
        command(["helm", "template", release, chart, "--namespace", namespace, *overrides])
        command(["kind", "load", "docker-image", image, "--name", cluster], timeout=900)
        with tempfile.TemporaryDirectory(prefix="grace-kind-") as temporary:
            token_path = Path(temporary) / "token"
            ensure_namespace(context, namespace, args.environment)
            with operation_lock(context, namespace, args.environment):
                deployment = check_release(context, namespace, release)
                ensure_token(context, namespace, args.environment, token_path)
                if deployment is not None:
                    smoke(context, namespace, release, python, token_path, args.environment, idle_only=True)
                command(["helm", "upgrade", "--install", release, chart,
                    "--kube-context", context, "--namespace", namespace, "--wait", "--timeout", "3m", *overrides], timeout=210)
                kube(context, "-n", namespace, "rollout", "status", f"deployment/{release}-control", "--timeout=60s")
                smoke(context, namespace, release, python, token_path, args.environment)
        print(f"PASS {context}. The simulation pod remains; temporary port-forward and token file removed.")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main()
    except (DemoError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        print("No cluster reset or automatic cleanup was attempted.", file=sys.stderr)
        sys.exit(1)
