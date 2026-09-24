"""Stage native-link's prebuilt proofs under the symbol names the loader uses."""
from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from micro.catalog import load_manifest


def stage_proofs(manifest: Path, programs: Path, proofs: Path, output: Path, objdump: str) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for benchmark in load_manifest(manifest).targets:
        base_name = str(benchmark.metadata["base_name"])
        native_object = programs / f"{base_name}.native.so"
        symbols_text = subprocess.check_output([objdump, "-t", str(native_object)], text=True)
        symbols = {line.rsplit(None, 1)[-1] for line in symbols_text.splitlines() if line.strip()}
        candidates = (base_name, f"{base_name}_xdp", f"{base_name}_prog")
        symbol = next((candidate for candidate in candidates if candidate in symbols), None)
        if symbol is None:
            raise RuntimeError(f"{native_object}: no native entry symbol from {candidates}")
        # run_micro_sim_batch builds this proof from the same native object and symbol.
        source = proofs / f"{benchmark.name}.proof.o"
        shutil.copyfile(source, output / f"{symbol}.proof.o")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--programs", type=Path, required=True)
    parser.add_argument("--proofs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--objdump", required=True)
    args = parser.parse_args()
    stage_proofs(args.manifest, args.programs, args.proofs, args.output, args.objdump)


if __name__ == "__main__":
    main()
