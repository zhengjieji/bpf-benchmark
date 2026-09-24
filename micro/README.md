# Micro Benchmark

`micro/` is the active isolated pure-JIT benchmark layer.

## Current Suite

- `micro/config/micro_pure_jit.yaml` is the active micro suite manifest.
- The suite covers the active workload-pattern benchmark set across staged XDP cases, packet-backed XDP controls, and a small kernel-only non-XDP control subset.
- The default benchmark set is intentionally workload-shaped rather than pass-pattern-shaped; pass-specific reduced cases belong in unit or regression tests.

## Directory Layout

- `driver.py`: consolidated micro suite driver; `make micro` is the canonical benchmark entrypoint
- `catalog.py`: micro-only suite YAML parser
- `../runner/libs/input_generators.py`: deterministic input generation for active benchmarks
- `../runner/`: shared C++ runner plus reusable Python libs for `micro/` and `corpus/`
- `programs/*.bpf.c`: active pure-JIT benchmark sources

## Build

Canonical preparation goes through the root `Makefile` (`make micro`) and the
Python local-prep pipeline.

## Usage

Run inside the framework-kernel VM:

```bash
make micro
```

Run a targeted VM benchmark with current knobs:

```bash
make micro BENCH=simple SAMPLES=1 WARMUPS=0 INNER_REPEAT=10
```

For an AWS comparison of the four pure-compute execution paths, use the smaller characterization image:

```bash
make micro PLATFORM=aws ARCH=x86 MICRO_RUNTIME_PROFILE=characterization RUNTIMES="kernel native_kernel native llvmbpf" SAMPLES=10 WARMUPS=0 WARMUP_REPEAT=5 INNER_REPEAT=100000 SHUFFLE_SEED=20260923
```

Set the `AWS_X86_*` or `AWS_ARM64_*` connection parameters for your account and an explicit AMI ID. Set `NATIVE_ARCH_CFLAGS` for the measurement CPU when building on another machine; the native build records the flags and compiler identity and rebuilds when either changes. The characterization profile packages these four paths and the default pure-JIT manifest. Other suites and optimization paths use the default full image.

`SAMPLES` counts fresh measured processes per runtime and benchmark. Each process executes `WARMUP_REPEAT` complete batches of `INNER_REPEAT` invocations before timing one further batch. `WARMUPS` retains its separate-process warmup meaning. The default in-process warmup count is five; explicit zero is supported. All four paths in one round use a recorded randomized order. Repeated invocations within a process are not independent statistical samples.

Kernel and native-kernel `exec_ns` use the kernel test-run duration; native and LLVM-BPF use a steady-clock execution-loop duration divided by the measured iteration count. Raw results include the actual iteration counts, warmup counts, order, timestamps, commands, artifact hashes, and execution identity. Statistical analysis and plotting belong outside the benchmark framework. The example sample count is a pilot setting, not a precision guarantee.

## LLVM KOperation Backend Path

The experimental LLVM backend path compiles the same `programs/*.bpf.c` sources
through clang IR plus a kop-enabled BPF `llc`, then runs the resulting objects
through the normal kernel micro runner.

Build or refresh the kop-enabled `llc`:

```bash
ninja -C llvm-backend/build-bpf-kop LLVMBPFCodeGen llc -j4
```

Generate kop-enabled micro objects:

```bash
out="micro/results/llvm_kop_programs_$(date +%Y%m%d_%H%M%S)"
make -C micro/programs \
  OUTPUT_DIR="$PWD/$out" \
  KERNEL_OFFSETS_INPUT="$PWD/micro/programs/build-x86/kernel_offsets.h" \
  BPFREJIT_MICRO_BPF_COMPILER=kop-llvm \
  BPF_KOP_LLC="$PWD/llvm-backend/build-bpf-kop/bin/llc" \
  all
```

Run those objects in the framework-kernel VM:

```bash
make micro TIMEOUT=7200 \
  MICRO_ARGS="--samples 3 --warmups 0 --inner-repeat 100000 --runtime kernel --program-dir $out"
```

Current smoke result from this path:

- kop object dir: `micro/results/llvm_kop_programs_20260518_014403`
- kop run: `micro/results/x86_kvm_micro_20260518_085229_761243`
- clang baseline: `micro/results/x86_kvm_micro_20260518_085516_282495`
- same-LLC no-selector control: `micro/results/x86_kvm_micro_20260518_085903_992103`
- correctness: 29/29 micro cases loaded and returned matching results
- selected koperation: `siphash_rotate64_mixer` 116, `payload_prefix_memcmp_scan` 2,
  `bpftrace_string_search_prefix_scan` 1
- selected-only effect vs same-LLC no-selector: `siphash_rotate64_mixer`
  78.7 ns -> 55.3 ns, `payload_prefix_memcmp_scan` 116.3 ns -> 108.0 ns,
  `bpftrace_string_search_prefix_scan` 180.0 ns -> 175.7 ns

Current limitation: the selector does not select koperation inside local `.text`
subprograms. With the current module proof stack ABI, selecting rotate koperation in
local-call callees can make verifier combined stack accounting exceed 512 bytes.

## Outputs

Results live under `micro/results/`.

- Each run lives under `micro/results/<run_type>_<timestamp>/`
- `metadata.json` is the canonical summary for that run
- `details/` contains `result.json` plus any retained per-sample payloads
- AWS machine and image snapshots are retained under `.cache/<target>/results/provenance/` after instance cleanup; join them to micro results using `RUN_TOKEN`.
