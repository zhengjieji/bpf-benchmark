"""Prevent stale native objects and proofs after changing micro compiler flags."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "native-sim"))
import run_micro_sim_batch as sim


class MicroBuildFlagsTests(unittest.TestCase):
    def test_changed_flags_or_compiler_rebuild_native_but_identical_build_does_not(self) -> None:
        make = shutil.which("gmake") or shutil.which("make")
        if make is None:
            self.skipTest("Make is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(REPO_ROOT / "micro/programs/Makefile", root / "Makefile")
            (root / "dispatch.mk").write_text('all:\n\t$(MAKE) -f Makefile "$(OUTPUT_DIR)/fixture.native.so"\n')
            (root / "fixture.bpf.c").write_text("int fixture(void) { return 0; }\n")
            (root / "offsets.h").write_text("/* fixture offsets */\n")
            compiler = root / "compiler"
            compiler.write_text(
                "#!/usr/bin/env python3\n"
                "import os, pathlib, sys\n"
                "if '--version' in sys.argv:\n"
                "    print(os.environ['FIXTURE_COMPILER_VERSION']); raise SystemExit(0)\n"
                "with pathlib.Path('compile.log').open('a') as stream: stream.write('compile\\n')\n"
                "pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text(' '.join(sys.argv[1:]))\n"
            )
            compiler.chmod(0o755)
            output = root / "out"
            target = output / "fixture.native.so"

            def build(flags: str, version: str = "fixture clang 1") -> int:
                command = [make, "-f", "dispatch.mk", "all", f"OUTPUT_DIR={output}", f"CLANG={compiler}",
                           f"KERNEL_OFFSETS_INPUT={root / 'offsets.h'}", "SYS_INCLUDE_FLAGS=",
                           "DEB_HOST_MULTIARCH=fixture", "NATIVE_ARCH=x86_64",
                           f"NATIVE_ARCH_CFLAGS={flags}"]
                subprocess.run(command, cwd=root, env={**os.environ, "FIXTURE_COMPILER_VERSION": version},
                               check=True, capture_output=True, text=True)
                return len((root / "compile.log").read_text().splitlines())

            first_flags = "-march=sapphirerapids -mno-red-zone -mgeneral-regs-only"
            self.assertEqual(build(first_flags), 1)
            self.assertIn("-march=sapphirerapids", target.read_text())
            stamp = output / ".native-build-flags"
            original_stamp_time = stamp.stat().st_mtime_ns
            self.assertEqual(build(first_flags), 1)
            self.assertEqual(stamp.stat().st_mtime_ns, original_stamp_time)
            self.assertEqual(build("-march=x86-64-v3 -mno-red-zone -mgeneral-regs-only"), 2)
            self.assertEqual(build("-march=x86-64-v3 -mno-red-zone -mgeneral-regs-only", "fixture clang 2"), 3)
            self.assertIn("fixture clang 2", stamp.read_text())

    def test_proof_input_matches_runtime_object_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native_so = root / "packet.native.so"
            native_o = root / "packet.native.o"
            native_so.write_bytes(b"shared object")
            native_o.write_bytes(b"relocatable object")
            self.assertEqual(sim.native_object_path("packet", root, stage2=False), native_so)
            self.assertEqual(sim.native_object_path("packet", root, stage2=True), native_o)
            native_so.unlink()
            with self.assertRaisesRegex(RuntimeError, "packet.native.so"):
                sim.native_object_path("packet", root, stage2=False)

    def test_proof_generation_records_the_binary_passed_to_native_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native_so = root / "packet.native.so"
            native_so.write_bytes(b"shared object to prove")
            linker = root / "native-link"
            linker.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                "source = pathlib.Path(sys.argv[sys.argv.index('--input') + 1])\n"
                "pathlib.Path(sys.argv[sys.argv.index('--output') + 1]).write_bytes(b'proof of ' + source.read_bytes())\n"
            )
            linker.chmod(0o755)
            config = sim.ARCHES["x86"]
            bench = sim.Bench("packet", "packet", 0, 2, "packet", False, False)
            real_require_ok = sim.require_ok

            def require_ok(command: list[str]) -> None:
                if command[0] != "cargo":
                    real_require_ok(command)

            with mock.patch.object(sim, "NATIVE_SIM_DIR", root), \
                    mock.patch.object(sim, "NATIVE_LINK_BIN", linker), \
                    mock.patch.object(sim, "native_entry_symbol", return_value="packet_xdp"), \
                    mock.patch.object(sim, "require_ok", side_effect=require_ok):
                proofs = sim.build_proof_objects(config, [bench], native_build_dir=root)
            metadata = json.loads((proofs / "packet.proof.json").read_text())
            self.assertEqual(metadata["native_object"], str(native_so))
            self.assertEqual(metadata["native_sha256"], hashlib.sha256(native_so.read_bytes()).hexdigest())
            self.assertEqual(metadata["proof_sha256"], hashlib.sha256((proofs / "packet.proof.o").read_bytes()).hexdigest())
            self.assertEqual(metadata["entry_symbol"], "packet_xdp")


if __name__ == "__main__":
    unittest.main()
