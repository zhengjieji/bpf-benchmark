"""Compile the real C++ parser/serializer and check repetition-count boundaries.

Run from the repository root:
    python3 -m unittest discover -s tests/python -p test_micro_cli.py -v

Requires a C++20 compiler (CXX, or c++). No Linux headers or BPF runtime are
needed; failures to compile remain test errors rather than skipped checks.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
COMMANDS = ("test-run", "run-native", "run-native-kernel", "run-llvmbpf")


class MicroCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory(prefix="micro-cli-test-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.executable = Path(cls.directory.name) / "micro_cli_harness"
        command = [
            *shlex.split(os.environ.get("CXX", "c++")),
            "-std=c++20", "-Wall", "-Wextra", "-Werror",
            "-DMICRO_EXEC_ENABLE_LLVMBPF", f"-I{REPO_ROOT / 'runner/include'}",
            str(REPO_ROOT / "runner/src/common.cpp"),
            str(REPO_ROOT / "tests/cpp/micro_cli_harness.cpp"),
            "-o", str(cls.executable),
        ]
        compiled = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if compiled.returncode:
            raise RuntimeError(f"C++ CLI harness compilation failed:\n{compiled.stdout}{compiled.stderr}")

    def parse(self, command: str, *extras: str) -> subprocess.CompletedProcess[str]:
        arguments = [str(self.executable), command, "--program", "fixture.bpf.o"]
        if command in ("run-native", "run-native-kernel"):
            arguments += ["--native-program", "fixture.native.o"]
        return subprocess.run([*arguments, *extras], capture_output=True, text=True, timeout=10)

    def accepted(self, command: str, *extras: str) -> dict:
        result = self.parse(command, *extras)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_default_warmup_for_all_four_commands(self) -> None:
        for command in COMMANDS:
            with self.subTest(command=command):
                self.assertEqual(self.accepted(command)["warmup_repeat"], 5)

    def test_zero_warmup_and_explicit_repeat(self) -> None:
        for command in COMMANDS:
            with self.subTest(command=command):
                self.assertEqual(self.accepted(command, "--warmup", "0", "--inner-repeat", "7"),
                                 {"warmup_repeat": 0, "repeat": 7})

    def test_uint32_upper_boundary_is_not_truncated(self) -> None:
        for command in COMMANDS:
            with self.subTest(command=command):
                self.assertEqual(self.accepted(command, "--warmup", "4294967295",
                                               "--inner-repeat", "4294967295"),
                                 {"warmup_repeat": 4294967295, "repeat": 4294967295})

    def test_negative_overflow_and_noninteger_counts_are_rejected(self) -> None:
        for command in COMMANDS:
            for option in ("--warmup", "--inner-repeat"):
                for value in ("-1", "4294967296", "1junk", "", "1.5"):
                    with self.subTest(command=command, option=option, value=value):
                        result = self.parse(command, option, value)
                        self.assertEqual(result.returncode, 1)
                        self.assertIn(f"{option} must be an integer", result.stderr)

    def test_zero_measured_repeat_is_rejected(self) -> None:
        for command in COMMANDS:
            with self.subTest(command=command):
                result = self.parse(command, "--inner-repeat", "0")
                self.assertEqual(result.returncode, 1)
                self.assertIn("--inner-repeat must be >= 1", result.stderr)

    def test_json_preserves_uint64_counts_and_timing_units(self) -> None:
        result = subprocess.run([str(self.executable), "serialize-counts"],
                                capture_output=True, text=True, check=True, timeout=10)
        sample = json.loads(result.stdout)
        self.assertEqual(sample["measured_iterations"], 4294967295)
        self.assertEqual(sample["warmup_batches"], 4294967295)
        self.assertEqual(sample["warmup_iterations"], 18446744065119617025)
        self.assertEqual(sample["timing_source"], "ktime")
        self.assertEqual(sample["timing_source_wall"], "clock_monotonic")
        self.assertEqual(sample["exec_cycles"], 123)
        self.assertEqual(sample["exec_cycles_source"], "tsc_ticks")
        self.assertEqual(sample["exec_cycles_scope"], "test_run_syscall_per_iteration")
        self.assertNotIn("tsc_freq_hz", sample)


if __name__ == "__main__":
    unittest.main()
