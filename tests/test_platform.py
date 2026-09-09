"""Repeatable source checks plus optional, offline Helm rendering tests.

Set GRACE_HELM_BIN to an approved Helm 3 executable to exercise template guards.
No test contacts Kubernetes, installs a release, or creates a container.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy/helm/grace"
CONTROL = CHART / "charts/grace-control/templates"
HELM = os.environ.get("GRACE_HELM_BIN") or shutil.which("helm")


class PlatformSourceTests(unittest.TestCase):
    def test_plain_yaml_and_child_dependencies(self):
        parsed = 0
        for path in (ROOT / "deploy").rglob("*.yaml"):
            source = path.read_text()
            if "{{" not in source:
                self.assertIsInstance(yaml.safe_load(source), dict, str(path))
                parsed += 1
        self.assertGreaterEqual(parsed, 11)
        dependencies = yaml.safe_load((CHART / "Chart.yaml").read_text())["dependencies"]
        self.assertEqual(len(dependencies), 3)
        for item in dependencies:
            child = CHART / "charts" / item["name"]
            metadata = yaml.safe_load((child / "Chart.yaml").read_text())
            self.assertEqual(metadata["name"], item["name"])
            self.assertEqual(metadata["version"], item["version"])
            self.assertEqual(item["repository"], f"file://charts/{item['name']}")
            self.assertTrue((child / "values.yaml").is_file())

    def test_unsafe_runtime_defaults_are_disabled(self):
        values = yaml.safe_load((CHART / "values.yaml").read_text())
        control = values["grace-control"]
        self.assertEqual(control["replicaCount"], 1)
        self.assertEqual(control["stateBackend"], "memory")
        self.assertEqual(control["mode"], "simulation")
        self.assertEqual(control["existingSecret"], "")
        self.assertFalse(control["allowProduction"])
        self.assertFalse(control["autoscaling"]["enabled"])
        self.assertFalse(values["grace-postgresql"]["enabled"])
        self.assertFalse(values["grace-execution"]["enabled"])

    def test_pod_security_probe_and_context_source_contracts(self):
        deployment = (CONTROL / "deployment.yaml").read_text()
        for required in (
            "type: Recreate", "automountServiceAccountToken: false",
            "runAsNonRoot: true", "readOnlyRootFilesystem: true",
            "type: RuntimeDefault", 'drop: ["ALL"]', "/health/live",
            "/health/ready", "GRACE_DEMO_TOKEN", "GRACE_STATE_BACKEND",
        ):
            self.assertIn(required, deployment)
        script = (ROOT / "scripts/Deploy-Simulation.ps1").read_text()
        self.assertIn("--kube-context $Context", script)
        self.assertIn("--context $Context", script)
        self.assertIn("if (-not $Apply)", script)


@unittest.skipUnless(HELM, "Helm unavailable: render/guard behavior remains unverified")
class PlatformHelmTests(unittest.TestCase):
    def helm(self, *arguments: str):
        # Explicitly avoid any user's cluster credential configuration.
        environment = dict(os.environ, KUBECONFIG=os.devnull)
        return subprocess.run(
            [str(HELM), *arguments], text=True, capture_output=True,
            timeout=30, check=False, env=environment,
        )

    def render(self, *arguments: str):
        return self.helm(
            "template", "grace-test", str(CHART), "--namespace", "grace-dev",
            "--set-string", "grace-control.existingSecret=render-only-placeholder",
            *arguments,
        )

    def test_helm_lint(self):
        result = self.helm(
            "lint", str(CHART), "--set-string",
            "grace-control.existingSecret=render-only-placeholder",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_rendered_simulation_pod_security(self):
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = [item for item in yaml.safe_load_all(result.stdout) if item]
        self.assertEqual(
            {item["kind"] for item in docs},
            {"Deployment", "Service", "ServiceAccount", "NetworkPolicy"},
        )
        deployment = next(item for item in docs if item["kind"] == "Deployment")
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["strategy"]["type"], "Recreate")
        pod = deployment["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        container = pod["containers"][0]
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        token = next(item for item in container["env"] if item["name"] == "GRACE_DEMO_TOKEN")
        self.assertEqual(token["valueFrom"]["secretKeyRef"]["name"], "render-only-placeholder")
        policy = next(item for item in docs if item["kind"] == "NetworkPolicy")
        self.assertEqual(policy["spec"]["ingress"], [])
        self.assertEqual(len(policy["spec"]["egress"]), 1)

    def test_unsafe_values_fail_template(self):
        values = (
            "grace-control.replicaCount=2", "grace-control.replicaCount=0",
            "grace-control.autoscaling.enabled=true", "grace-control.pdb.enabled=true",
            "grace-control.stateBackend=postgres", "grace-control.mode=live",
            "grace-control.allowProduction=true", "grace-control.environment=production",
            "grace-control.service.type=LoadBalancer", "grace-execution.enabled=true",
            "grace-postgresql.enabled=true",
        )
        for override in values:
            with self.subTest(override=override):
                result = self.render("--set", override)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("Error:", result.stderr)

    def test_missing_token_secret_and_production_overlay_fail(self):
        result = self.helm("template", "test", str(CHART))
        self.assertNotEqual(result.returncode, 0)
        result = self.render(
            "--values", str(ROOT / "deploy/environments/production-blocked.yaml"),
        )
        self.assertNotEqual(result.returncode, 0)

    def test_optional_postgres_requires_explicit_dev_backup_ack(self):
        allowed = (
            "--set", "grace-postgresql.enabled=true",
            "--set", "grace-postgresql.backupAcknowledged=true",
            "--set-string", "grace-postgresql.existingSecret=render-only-db-secret",
        )
        result = self.render(*allowed)
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = [item for item in yaml.safe_load_all(result.stdout) if item]
        database = next(item for item in docs if item["kind"] == "StatefulSet")
        self.assertEqual(database["spec"]["replicas"], 1)
        self.assertEqual(len(database["spec"]["volumeClaimTemplates"]), 1)
        forbidden = self.render(*allowed, "--set", "grace-postgresql.environment=qa")
        self.assertNotEqual(forbidden.returncode, 0)


if __name__ == "__main__":
    unittest.main()
