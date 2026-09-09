"""Offline guard tests plus the same smoke client against real local transports."""
import argparse
import asyncio
import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
from grace.transport.grpc_server import build_server
from grace.transport.rest import create_app
from grace.transport.server import simulation_observer, simulation_service

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


demo = load_script("kind-demo")
smoke = load_script("smoke-api")


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def node(name, arch="arm64", ready="True"):
    return {"metadata": {"name": name}, "status": {
        "nodeInfo": {"operatingSystem": "linux", "architecture": arch},
        "conditions": [{"type": "Ready", "status": ready}]}}


class KindGuardTests(unittest.TestCase):
    def test_explicit_target_required_and_production_environment_rejected(self):
        for args in ([], ["--context", "kind-test", "--environment", "prod"],
                     ["--list", "--cleanup"], ["--skip-build", "--context", "kind-test"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                demo.arguments(args)

    def test_non_kind_and_production_named_contexts_rejected(self):
        for name in ("gke-test", "kind-production", "kind-prod-us", "kind-platinum"):
            with self.assertRaises(demo.DemoError):
                demo.select_targets(argparse.Namespace(context=name),
                                    {"kind-dev": "dev", "kind-production": "production",
                                     "kind-prod-us": "prod-us", "kind-platinum": "platinum"})

    def test_list_is_read_only_and_exact_contexts_only(self):
        calls = []
        def execute(args, **kwargs):
            calls.append(args)
            if args[:3] == ["kind", "get", "clusters"]:
                return completed("demo\nprod\nmissing-context\n")
            return completed("kind-demo\nkind-prod\ngke-team\n")
        with patch.object(demo, "required"), patch.object(demo, "command", side_effect=execute), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            demo.main(["--list"])
        self.assertEqual(len(calls), 2)
        self.assertIn("kind-demo", output.getvalue())
        self.assertNotIn("missing-context", output.getvalue())
        self.assertIn("REFUSED", output.getvalue())

    def test_mixed_architecture_and_unready_nodes_fail_before_build(self):
        for nodes in ([node("a"), node("b", "amd64")], [node("a"), node("b", ready="False")]):
            def execute(args, **kwargs):
                if args[0] == "kind":
                    return completed("a\nb\n")
                return completed("demo\n")
            with patch.object(demo, "command", side_effect=execute), \
                 patch.object(demo, "kube", return_value=completed(json.dumps({"items": nodes}))), \
                 self.assertRaises(demo.DemoError):
                demo.inspect_target("kind-demo", "demo")

    def test_node_names_and_docker_ownership_are_verified(self):
        with patch.object(demo, "command", return_value=completed("real-kind-node\n")), \
             patch.object(demo, "kube", return_value=completed(json.dumps({"items": [node("remote-node")]}))), \
             self.assertRaisesRegex(demo.DemoError, "node names"):
            demo.inspect_target("kind-demo", "demo")
        with patch.object(demo, "command", side_effect=[completed("a\n"), completed("other\n")]), \
             patch.object(demo, "kube", return_value=completed(json.dumps({"items": [node("a")]}))), \
             self.assertRaisesRegex(demo.DemoError, "ownership"):
            demo.inspect_target("kind-demo", "demo")

    def test_owned_namespace_and_secret_required(self):
        with patch.object(demo, "get_resource", return_value={"metadata": {"labels": {}}}), \
             patch.object(demo, "kube") as kube:
            with self.assertRaises(demo.DemoError):
                demo.ensure_namespace("kind-demo", "grace-kind-dev", "dev")
            with self.assertRaises(demo.DemoError):
                demo.ensure_token("kind-demo", "grace-kind-dev", "dev", Path("unused"))
            kube.assert_not_called()

    def test_secret_never_passed_as_argument_and_file_permissions_private(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(demo, "get_resource", return_value=None), \
             patch.object(demo, "kube", return_value=completed()) as kube:
            path = Path(directory) / "token"
            demo.ensure_token("kind-demo", "grace-kind-dev", "dev", path)
            token = path.read_text()
            self.assertGreaterEqual(len(token), 32)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            call = kube.call_args
            self.assertNotIn(token, repr(call.args))
            self.assertTrue(call.kwargs["sensitive"])
            encoded = json.loads(call.kwargs["stdin"])["data"]["demo-token"]
            self.assertEqual(base64.b64decode(encoded).decode(), token)

    def test_sensitive_errors_redacted_and_timeout_bounded(self):
        with patch.object(demo.subprocess, "run", return_value=completed(returncode=1, stderr="SECRET-VALUE")):
            with self.assertRaises(demo.DemoError) as error:
                demo.command(["kubectl", "create"], stdin="SECRET-VALUE", sensitive=True)
            self.assertNotIn("SECRET-VALUE", str(error.exception))
        with patch.object(demo.subprocess, "run", side_effect=subprocess.TimeoutExpired("docker", 90)):
            with self.assertRaisesRegex(demo.DemoError, "deadline"):
                demo.command(["docker", "inspect"])

    def test_lock_not_deleted_if_another_operation_owns_it(self):
        with patch.object(demo, "kube", return_value=completed(returncode=1)) as kube:
            with self.assertRaises(demo.DemoError):
                with demo.operation_lock("kind-demo", "grace-kind-dev", "dev"):
                    self.fail("lock should fail")
            self.assertEqual(kube.call_count, 1)

    def test_lock_released_on_failure(self):
        with patch.object(demo, "kube", return_value=completed()) as kube:
            with self.assertRaisesRegex(RuntimeError, "injected"):
                with demo.operation_lock("kind-demo", "grace-kind-dev", "dev"):
                    raise RuntimeError("injected")
            self.assertIn("delete", kube.call_args.args)
            self.assertIn("configmap", kube.call_args.args)

    def test_foreign_helm_release_rejected(self):
        releases = [{"name": "grace-dev", "namespace": "grace-kind-dev", "chart": "unrelated-1.0"}]
        with patch.object(demo, "command", return_value=completed(json.dumps(releases))), \
             self.assertRaisesRegex(demo.DemoError, "parent chart"):
            demo.check_release("kind-demo", "grace-kind-dev", "grace-dev")

    def test_cleanup_cannot_touch_foreign_namespace(self):
        with patch.object(demo, "get_resource", return_value={"metadata": {}}), \
             patch.object(demo, "command") as command:
            with self.assertRaises(demo.DemoError):
                demo.cleanup("kind-demo", "grace-kind-dev", "grace-dev", "dev")
            command.assert_not_called()

    def test_image_option_rejects_shell_like_or_ambiguous_values(self):
        for image in ("$(id)", "registry:5000/image:dev", "no-tag", "image:tag;echo"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                demo.arguments(["--context", "kind-test", "--skip-build", "--image", image])


class ActualSmokeClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        token_path = Path(self.temporary.name) / "token"
        token_path.write_text("loopback-test-token-not-a-deployment-secret-1234")
        self.service, gpus, controller = simulation_service({"GRACE_DEMO_TOKEN": token_path.read_text()})
        self.caller = self.service.authenticate("Bearer " + token_path.read_text())
        self.runner = web.AppRunner(create_app(self.service))
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        http_port = self.site._server.sockets[0].getsockname()[1]
        self.grpc = build_server(self.service)
        grpc_port = self.grpc.add_insecure_port("127.0.0.1:0")
        await self.grpc.start()
        self.observer = asyncio.create_task(simulation_observer(self.service, gpus, controller))
        self.args = argparse.Namespace(http=f"http://127.0.0.1:{http_port}", grpc=f"127.0.0.1:{grpc_port}",
            token_file=str(token_path), environment="dev", assert_idle_only=False)

    async def asyncTearDown(self):
        self.observer.cancel()
        await asyncio.gather(self.observer, return_exceptions=True)
        await self.grpc.stop(0)
        await self.runner.cleanup()
        self.temporary.cleanup()

    async def test_complete_client_uses_real_rest_grpc_and_observer(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            await smoke.run(self.args)
        self.assertIn("full-device reuse", output.getvalue())
        self.assertTrue(all(item.state.value in smoke.TERMINAL for item in self.service.engine.list(self.caller)))

    async def test_idle_preflight_rejects_active_without_cancelling_it(self):
        item = self.service.create({"tenant_id": "demo", "application_id": "existing-user",
            "gpu_type": "A100-40GB", "gpu_memory_mib": 5120, "gpu_millicards": 250,
            "data_locations": ["onprem"]}, self.caller, "existing-request")
        self.args.assert_idle_only = True
        with self.assertRaisesRegex(RuntimeError, "Existing active reservations"):
            await smoke.run(self.args)
        self.assertEqual(self.service.get(item.id, self.caller).state.value, "reserved")
