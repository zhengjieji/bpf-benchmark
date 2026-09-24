"""Regression tests for durable EC2 identity and snapshot failure propagation."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from micro import driver
from runner.libs import aws_common, aws_executor, aws_provenance
from runner.libs.run_contract import (
    ArtifactRequirements, AwsConfig, KvmConfig, RemoteConfig, RunConfig, RunIdentity, SuiteRequirements,
)


class AwsProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="aws-provenance-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        contract = RunConfig(
            RunIdentity("aws-x86", "x86_64", "aws", "micro", "test-token"),
            SuiteRequirements(), ArtifactRequirements(),
            RemoteConfig(user="ec2-user", stage_dir="/tmp/measurement", python_bin="python3",
                         runtime_container_image="bpf-benchmark/micro-characterization:x86_64"),
            AwsConfig(region="us-east-1"), KvmConfig(),
        )
        self.ctx = aws_common.AwsExecutorContext(
            "run", contract, "aws-x86", "micro", "test-token", "ec2-user", "/tmp/measurement",
            self.root / "not-a-key", "us-east-1", "test-profile", self.root,
            self.root / "run-state" / "test-token", self.root / "run-state" / "test-token" / "instance.json",
            self.root / "results",
        )
        self.instance = {"InstanceId": "i-test", "ImageId": "ami-test", "InstanceType": "t3.small",
                         "LaunchTime": "2026-09-23T00:00:00Z", "CpuOptions": {"CoreCount": 1}}
        self.machine = {"runtime_image": {"Id": "sha256:test"}, "files": [
            {"path": "/proc/sys/kernel/random/boot_id", "status": "present", "text": "boot-test\n"}]}

    def provision(self, ctx) -> str:
        aws_executor._save_state(ctx, instance_id="i-test", instance_ip="192.0.2.1", kernel_release="test-kernel")
        return "192.0.2.1"

    def terminate(self, ctx, instance_id) -> None:
        self.assertEqual(instance_id, "i-test")
        ctx.state_file.unlink(missing_ok=True)

    def patch_cloud(self):
        return mock.patch.multiple(
            aws_common,
            _aws_cmd=mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(self.instance), "")),
            _ssh_exec=mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(self.machine), "")),
            _terminate_instance=mock.Mock(side_effect=self.terminate),
        )

    def run_dir(self) -> Path:
        directories = list((self.ctx.results_dir / "provenance").glob("micro_test-token_*"))
        self.assertEqual(len(directories), 1)
        return directories[0]

    def test_success_keeps_identity_after_disposable_state_is_removed(self) -> None:
        with self.patch_cloud(), \
                mock.patch.object(aws_executor, "_ensure_instance_for_suite", side_effect=self.provision), \
                mock.patch.object(aws_executor, "_run_remote_suite"):
            aws_executor._run_aws(self.ctx)
        self.assertFalse(self.ctx.run_state_dir.exists())
        directory = self.run_dir()
        self.assertEqual(json.loads((directory / "execution.json").read_text())["status"], "completed")
        for phase in ("before", "after"):
            snapshot = json.loads((directory / f"{phase}.json").read_text())
            self.assertEqual(snapshot["instance"], self.instance)
            self.assertEqual(snapshot["machine"], self.machine)
            self.assertEqual(snapshot["run_token"], "test-token")
            self.assertEqual(snapshot["status"], "completed")

    def test_other_suites_and_images_keep_the_existing_execution_path(self) -> None:
        for suite_name, image in (("micro", "bpf-benchmark/runner-runtime:x86_64"),
                                  ("corpus", "bpf-benchmark/micro-characterization:x86_64"),
                                  ("test", "")):
            contract = replace(self.ctx.contract, remote=replace(self.ctx.contract.remote,
                                                                runtime_container_image=image))
            ctx = replace(self.ctx, suite_name=suite_name, contract=contract)
            with self.subTest(suite=suite_name, image=image), self.patch_cloud(), \
                    mock.patch.object(aws_executor, "_ensure_instance_for_suite", side_effect=self.provision), \
                    mock.patch.object(aws_executor, "_run_remote_suite") as suite, \
                    mock.patch.object(aws_executor, "capture_snapshot") as capture:
                aws_executor._run_aws(ctx)
                suite.assert_called_once_with(ctx, "192.0.2.1")
                capture.assert_not_called()
                self.assertFalse((self.ctx.results_dir / "provenance").exists())

    def test_failed_suite_preserves_identity_and_original_error(self) -> None:
        with self.patch_cloud(), \
                mock.patch.object(aws_executor, "_ensure_instance_for_suite", side_effect=self.provision), \
                mock.patch.object(aws_executor, "_run_remote_suite", side_effect=RuntimeError("suite failed")):
            with self.assertRaisesRegex(RuntimeError, "suite failed"):
                aws_executor._run_aws(self.ctx)
        directory = self.run_dir()
        record = json.loads((directory / "execution.json").read_text())
        failure = json.loads((directory / "failure.json").read_text())
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["error"], "suite failed")
        self.assertEqual(failure["instance"]["InstanceId"], "i-test")
        self.assertFalse(self.ctx.state_file.exists())

    def test_mandatory_host_snapshot_failure_is_not_silenced(self) -> None:
        with self.patch_cloud(), \
                mock.patch.object(aws_executor, "_ensure_instance_for_suite", side_effect=self.provision), \
                mock.patch.object(aws_executor, "_run_remote_suite") as suite, \
                mock.patch.object(aws_common, "_ssh_exec", return_value=
                                  subprocess.CompletedProcess([], 1, "", "lscpu failed")):
            with self.assertRaisesRegex(RuntimeError, "lscpu failed"):
                aws_executor._run_aws(self.ctx)
            suite.assert_not_called()
        snapshot = json.loads((self.run_dir() / "before.json").read_text())
        self.assertEqual(snapshot["status"], "error")
        self.assertEqual(snapshot["instance"]["ImageId"], "ami-test")
        self.assertIn("lscpu failed", snapshot["error"])

    def test_post_run_snapshot_failure_keeps_already_synced_raw_results(self) -> None:
        raw_result = self.root / "micro" / "results" / "raw.json"

        def sync_results(ctx, ip):
            raw_result.parent.mkdir(parents=True)
            raw_result.write_text('{"exec_ns": 123}\n')

        with self.patch_cloud(), \
                mock.patch.object(aws_executor, "_ensure_instance_for_suite", side_effect=self.provision), \
                mock.patch.object(aws_executor, "_run_remote_suite", side_effect=sync_results), \
                mock.patch.object(aws_common, "_ssh_exec", side_effect=(
                    subprocess.CompletedProcess([], 0, json.dumps(self.machine), ""),
                    subprocess.CompletedProcess([], 1, "", "post-run snapshot failed"))):
            with self.assertRaisesRegex(RuntimeError, "post-run snapshot failed"):
                aws_executor._run_aws(self.ctx)
        self.assertEqual(json.loads(raw_result.read_text()), {"exec_ns": 123})
        snapshot = json.loads((self.run_dir() / "after.json").read_text())
        self.assertEqual(snapshot["status"], "error")
        self.assertEqual(snapshot["instance"]["InstanceId"], "i-test")

    def test_optional_absence_differs_from_read_failure_and_preserves_bytes(self) -> None:
        missing = self.root / "missing"
        self.assertEqual(aws_provenance._snapshot_file(str(missing))["status"], "absent")
        with self.assertRaises(FileNotFoundError):
            aws_provenance._snapshot_file(str(missing), required=True)
        data = self.root / "config"
        data.write_bytes(b"CONFIG_BPF=y\n")
        snapshot = aws_provenance._snapshot_file(str(data), required=True)
        self.assertEqual(snapshot["text"], "CONFIG_BPF=y\n")
        self.assertEqual(snapshot["sha256"], hashlib.sha256(data.read_bytes()).hexdigest())
        with mock.patch.object(Path, "open", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                aws_provenance._snapshot_file(str(data))

    def test_remote_scripts_compile_and_aws_projection_excludes_sensitive_fields(self) -> None:
        host_script, image_script = aws_provenance._remote_scripts()
        compile(host_script, "host-snapshot", "exec")
        compile(image_script, "image-snapshot", "exec")
        for field in ("InstanceId", "ImageId", "InstanceType", "CpuOptions", "Placement", "LaunchTime"):
            self.assertIn(field, aws_provenance.INSTANCE_QUERY)
        for field in ("UserData", "Credentials", "Tags", "IamInstanceProfile"):
            self.assertNotIn(field, aws_provenance.INSTANCE_QUERY)

    def test_micro_result_links_the_executor_token_and_exact_kernel_config(self) -> None:
        config = self.root / "kernel.config"
        config.write_bytes(b"CONFIG_BPF=y\n")

        def artifact_path(path):
            if path == "/artifacts/kernel/config":
                return config
            if path == "/artifacts/source-manifest.json":
                return self.root / "missing-source-manifest"
            return Path(path)

        env = {"RUN_TOKEN": "test-token", "RUN_EXECUTOR": "aws", "RUN_TARGET_NAME": "aws-x86",
               "RUN_TARGET_ARCH": "x86_64"}
        with mock.patch.dict(os.environ, env), mock.patch.object(driver, "Path", side_effect=artifact_path), \
                mock.patch.object(driver, "_git_rev_parse", return_value="unknown"), \
                mock.patch.object(driver, "_git_is_dirty", return_value=False), \
                mock.patch.object(driver, "_read_cpu_model", return_value="fixture"), \
                mock.patch.object(driver, "_detect_environment", return_value="vm"):
            provenance = driver.collect_provenance(driver.parse_args([]), 10, 0, 100000)
        self.assertEqual(provenance["execution"], {"run_token": "test-token", "executor": "aws",
                                                   "target": "aws-x86", "target_arch": "x86_64"})
        self.assertEqual(provenance["kernel_config"]["sha256"], hashlib.sha256(config.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
