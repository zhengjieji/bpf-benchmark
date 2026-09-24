"""Regressions for warmup forwarding, complete rounds, and raw run records."""
from __future__ import annotations

import json
import hashlib
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from micro import driver
from micro.catalog import CatalogBuild, CatalogManifest, CatalogRuntime, CatalogTarget, DefaultsSpec
from runner.libs.run_contract import (
    ArtifactRequirements, AwsConfig, KvmConfig, RemoteConfig, RunConfig, RunIdentity, SuiteRequirements,
)
from runner.libs.suite_commands import build_runtime_container_command
from runner.libs.stage_micro_proofs import stage_proofs
from runner.libs.workspace_layout import runtime_container_image_tar_path
from runner.suites import micro as micro_suite


RUNTIME_NAMES = ("kernel", "native_kernel", "native", "llvmbpf")


class MicroSamplingTests(unittest.TestCase):
    def test_characterization_image_rejects_unsupported_runtime_and_manifest(self) -> None:
        env = {
            "RUN_TARGET_NAME": "x86", "RUN_TARGET_ARCH": "x86_64", "RUN_EXECUTOR": "aws",
            "RUN_TOKEN": "test", "RUN_REMOTE_PYTHON_BIN": "python3",
            "BPFREJIT_MICRO_IMAGE_PROFILE": "characterization",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            args = micro_suite._parse_args([])
            args.runtimes = list(RUNTIME_NAMES)
            self.assertEqual(micro_suite._selected_runtimes(args), list(RUNTIME_NAMES))
            for runtime in ("kernel_rejit", "native_proof"):
                args.runtimes = [runtime]
                with self.subTest(runtime=runtime), self.assertRaises(SystemExit):
                    micro_suite._selected_runtimes(args)
            args.runtimes = list(RUNTIME_NAMES)
            args.suite = "/tmp/custom/micro_pure_jit.yaml"
            with self.assertRaises(SystemExit):
                micro_suite._selected_runtimes(args)
        full = runtime_container_image_tar_path(REPO_ROOT, "x86_64", runtime_image="bpf-benchmark/runner-runtime:x86_64")
        micro = runtime_container_image_tar_path(REPO_ROOT, "x86_64", runtime_image="bpf-benchmark/micro-characterization:x86_64")
        self.assertEqual(full.name, "x86_64-runner-runtime.image.tar")
        self.assertEqual(micro.name, "x86_64-micro-characterization.image.tar")

    def test_native_proof_staging_uses_elf_entry_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "proofs"
            source.mkdir()
            (source / "packet.proof.o").write_bytes(b"packet proof")
            (root / "packet.native.so").write_bytes(b"native packet")
            (source / "packet.proof.json").write_text(json.dumps({
                "native_sha256": hashlib.sha256(b"native packet").hexdigest(),
                "entry_symbol": "packet_xdp",
                "proof_sha256": hashlib.sha256(b"packet proof").hexdigest(),
            }))
            manifest = root / "micro.yaml"
            manifest.write_text(
                "suite_name: fixture\npaths: {program_dir: programs}\n"
                "runtimes: [{name: native}]\nbenchmarks: [{name: packet, base_name: packet}]\n"
            )
            with mock.patch("runner.libs.stage_micro_proofs.subprocess.check_output", return_value=
                            "0000000000001100 g F xdp 0000000000000050 packet_xdp\n"):
                stage_proofs(manifest, root, source, root / "staged", "objdump")
            self.assertEqual((root / "staged" / "packet_xdp.proof.o").read_bytes(), b"packet proof")
            self.assertFalse((root / "staged" / "packet.proof.o").exists())
            (root / "packet.native.so").write_bytes(b"native recompiled with other flags")
            with mock.patch("runner.libs.stage_micro_proofs.subprocess.check_output", return_value=
                            "0000000000001100 g F xdp 0000000000000050 packet_xdp\n"):
                with self.assertRaisesRegex(RuntimeError, "different native object"):
                    stage_proofs(manifest, root, source, root / "staged", "objdump")
            with mock.patch("runner.libs.stage_micro_proofs.subprocess.check_output", return_value="other_symbol\n"):
                with self.assertRaisesRegex(RuntimeError, "no native entry symbol"):
                    stage_proofs(manifest, root, source, root / "staged", "objdump")

    def test_zero_inner_warmup_survives_suite_and_helper_cli(self) -> None:
        # Dropping a zero value would silently reactivate the helper's default warmup.
        env = {
            "RUN_TARGET_NAME": "x86", "RUN_TARGET_ARCH": "x86_64", "RUN_EXECUTOR": "aws",
            "RUN_TOKEN": "test", "RUN_REMOTE_PYTHON_BIN": "python3", "WARMUPS": "2",
            "WARMUP_REPEAT": "0", "INNER_REPEAT": "97", "RUNTIMES": " ".join(RUNTIME_NAMES),
        }
        with mock.patch.dict(os.environ, env, clear=True):
            suite_args = micro_suite._parse_args([])
            driver_args = driver.parse_args(micro_suite._micro_driver_argv(REPO_ROOT, suite_args))
        self.assertEqual(driver_args.warmups, 2)
        self.assertEqual(driver_args.warmup_repeat, 0)
        benchmark = CatalogTarget("test", Path("test.bpf.o"), native_object_path=Path("test.so"),
                                  native_kernel_object_path=Path("test.so"))
        for runtime_name in RUNTIME_NAMES:
            command = driver.build_runner_command(
                runner_binary=Path("micro_exec"), benchmark=benchmark,
                runtime=CatalogRuntime(runtime_name), inner_repeat=driver_args.inner_repeat,
                warmup_repeat=driver_args.warmup_repeat, perf_counters=False, memory_file=None, cpu="1",
            )
            self.assertEqual(command[:3], ["taskset", "-c", "1"])
            self.assertEqual(command[command.index("--warmup") + 1], "0")
            self.assertEqual(command[command.index("--inner-repeat") + 1], "97")

    def test_container_forwards_explicit_zero_warmup(self) -> None:
        config = RunConfig(
            identity=RunIdentity("x86", "x86_64", "aws", "micro", "test"),
            suite=SuiteRequirements(), artifacts=ArtifactRequirements(),
            remote=RemoteConfig(python_bin="python3", runtime_container_image="test-image"),
            aws=AwsConfig(), kvm=KvmConfig(),
        )
        with mock.patch.dict(os.environ, {"WARMUP_REPEAT": "0"}, clear=True):
            command = build_runtime_container_command(REPO_ROOT, config, die=self.fail)
        self.assertIn("WARMUP_REPEAT=0", command)

    def test_protocol_rejects_mixed_or_missing_warmup_metadata(self) -> None:
        sample = {"measured_iterations": 97, "warmup_batches": 3, "warmup_iterations": 291,
                  "timing_source": "steady_clock_batch"}
        driver.validate_sample_protocol(sample, warmup_repeat=3)
        for field, value in (("warmup_batches", 1), ("warmup_iterations", 3),
                             ("measured_iterations", 0), ("timing_source", "")):
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                driver.validate_sample_protocol({**sample, field: value}, warmup_repeat=3)

    def test_seed_changes_order_and_benchmark_name_separates_streams(self) -> None:
        runtimes = [CatalogRuntime(name) for name in RUNTIME_NAMES]

        def orders(seed: int, name: str) -> list[list[str]]:
            return [[runtime.name for runtime in driver.runtime_order_for_round(
                runtimes, seed=seed, benchmark_name=name, round_index=index)] for index in range(8)]

        first = orders(7, "fnv")
        self.assertEqual(first, orders(7, "fnv"))
        self.assertNotEqual(first, orders(8, "fnv"))
        self.assertNotEqual(first, orders(7, "checksum"))
        for order in first:
            self.assertCountEqual(order, RUNTIME_NAMES)

    def test_driver_preserves_rounds_and_failed_sample_payload(self) -> None:
        # Run the driver against a tiny executable fixture: this exercises actual
        # subprocess argv and JSON serialization, without requiring Linux BPF.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "micro_exec"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "if '--help' in sys.argv:\n"
                "    print('run-llvmbpf'); raise SystemExit(0)\n"
                "warmup = int(sys.argv[sys.argv.index('--warmup') + 1])\n"
                "repeat = int(sys.argv[sys.argv.index('--inner-repeat') + 1])\n"
                "print(json.dumps({'result': 42, 'retval': 0, 'exec_ns': 11, 'repeat': repeat, "
                "'measured_iterations': repeat, 'warmup_batches': warmup, "
                "'warmup_iterations': warmup * repeat, 'timing_source': 'fixture'}))\n"
            )
            executable.chmod(0o755)
            object_path = root / "fixture.o"
            object_path.write_bytes(b"fixture")
            manifest_path = root / "suite.yaml"
            manifest_path.write_text("fixture: true\n")
            target = CatalogTarget("fixture", object_path, native_object_path=object_path,
                                   native_kernel_object_path=object_path,
                                   proof_object_path=object_path, proof_compile_metadata_path=object_path,
                                   expected_result=42, expected_retval=0)
            suite = CatalogManifest(
                manifest_path, "fixture", DefaultsSpec(3, 1, 97, RUNTIME_NAMES, root / "result.json"),
                CatalogBuild(executable), tuple(CatalogRuntime(name, 97) for name in RUNTIME_NAMES),
                (target,),
            )
            with mock.patch.object(driver, "load_suite", return_value=suite), \
                    mock.patch.object(driver, "resolve_memory_file", return_value=None), \
                    mock.patch.object(driver, "read_required_text", return_value="fixture"), \
                    mock.patch.object(driver, "validate_publication_environment"), \
                    mock.patch.object(driver, "write_code_compare_markdown"):
                status = driver.main(["--warmup-repeat", "2", "--shuffle-seed", "7"])
            self.assertEqual(status, 0)
            payload = json.loads(next(root.glob("result_*/details/result.json")).read_text())
            benchmark = payload["benchmarks"][0]
            self.assertEqual(len(benchmark["warmup_runs"]), 4)
            self.assertEqual(len(benchmark["rounds"]), 3)
            for round_record in benchmark["rounds"]:
                self.assertEqual(round_record["status"], "completed")
                self.assertCountEqual(round_record["runtime_order"], RUNTIME_NAMES)
            for run in benchmark["runs"]:
                self.assertEqual([sample["sample_index"] for sample in run["samples"]], [0, 1, 2])
                for sample in run["samples"]:
                    round_record = benchmark["rounds"][sample["round_index"]]
                    self.assertEqual(round_record["runtime_order"][sample["runtime_order_index"]], run["runtime"])
                    self.assertEqual(sample["warmup_iterations"], 194)
                    self.assertGreater(sample["process_elapsed_ns"], 0)
                    self.assertLessEqual(sample["process_started_at"], sample["process_completed_at"])
                    self.assertEqual(sample["command"][sample["command"].index("--warmup") + 1], "2")
            self.assertEqual(len(benchmark["files"]["bpf_object"]["sha256"]), 64)

            bad_suite = replace(suite, targets=(replace(target, expected_result=43),))
            with mock.patch.object(driver, "load_suite", return_value=bad_suite), \
                    mock.patch.object(driver, "resolve_memory_file", return_value=None), \
                    mock.patch.object(driver, "read_required_text", return_value="fixture"), \
                    mock.patch.object(driver, "validate_publication_environment"), \
                    mock.patch.object(driver, "write_code_compare_markdown"):
                status = driver.main(["--warmup-repeat", "0", "--warmups", "0",
                                      "--output", str(root / "failed.json")])
            self.assertEqual(status, 1)
            failed = json.loads(next(root.glob("failed_*/details/result.json")).read_text())["benchmarks"][0]
            self.assertEqual(failed["rounds"][0]["status"], "error")
            self.assertEqual(len(failed["runs"]), 1)
            self.assertEqual(failed["runs"][0]["samples"][0]["result"], 42)
            self.assertIn("result mismatch", failed["error"])


if __name__ == "__main__":
    unittest.main()
