"""Durable measurement-machine snapshots, independent of disposable run state."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from runner.libs import aws_common
from runner.libs.state_file import write_json_object


# Explicit projection excludes credentials, user data, tags, and image environment.
INSTANCE_QUERY = (
    "Reservations[0].Instances[0].{InstanceId:InstanceId,ImageId:ImageId,"
    "InstanceType:InstanceType,Architecture:Architecture,LaunchTime:LaunchTime,"
    "Placement:Placement,CpuOptions:CpuOptions,EbsOptimized:EbsOptimized,"
    "VirtualizationType:VirtualizationType,Hypervisor:Hypervisor,"
    "BootMode:BootMode,CurrentInstanceBootMode:CurrentInstanceBootMode,"
    "EnaSupport:EnaSupport,RootDeviceType:RootDeviceType,State:State}"
)


def _snapshot_file(path: str, *, required: bool = False, text: bool = True) -> dict:
    source = Path(path)
    digest = hashlib.sha256()
    chunks = []
    size = 0
    try:
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
                if text:
                    chunks.append(chunk)
    except FileNotFoundError:
        if required:
            raise
        return {"path": path, "status": "absent"}
    record = {"path": path, "status": "present", "size_bytes": size, "sha256": digest.hexdigest()}
    if text:
        record["text"] = b"".join(chunks).decode("utf-8")
    return record


def _snapshot_command(command: list[str]) -> str:
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode:
        raise RuntimeError(f"snapshot command failed ({completed.returncode}): {command[0]}: {completed.stderr.strip()}")
    return completed.stdout


def _image_snapshot() -> dict:
    return {
        "files": [_snapshot_file(path) for path in (
            "/artifacts/manifest.json", "/artifacts/source-manifest.json",
            "/artifacts/kernel/config", "/artifacts/kernel/kernel_offsets.h",
        )],
        "kernel_images": [_snapshot_file(path, text=False) for path in (
            "/artifacts/kernel/bzImage", "/artifacts/kernel/vmlinuz.efi",
        )],
    }


def _machine_snapshot(image_name: str, image_python: str, image_script: str) -> dict:
    uname = platform.uname()
    required_files = ("/etc/os-release", "/proc/cpuinfo", "/proc/version",
                      "/proc/cmdline", "/proc/sys/kernel/random/boot_id")
    optional_files = (
        "/sys/devices/system/cpu/online", "/sys/devices/system/cpu/possible",
        "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor",
        "/sys/devices/system/cpu/intel_pstate/no_turbo",
        "/sys/devices/system/cpu/cpufreq/boost",
        "/sys/devices/system/clocksource/clocksource0/current_clocksource",
        "/proc/sys/net/core/bpf_jit_enable", "/proc/sys/net/core/bpf_jit_harden",
        "/proc/sys/kernel/bpf_stats_enabled", "/proc/sys/kernel/perf_event_paranoid",
        f"/boot/config-{uname.release}",
    )
    image = json.loads(_snapshot_command(["docker", "image", "inspect", image_name]))[0]
    identity = {name: image.get(name) for name in (
        "Id", "RepoTags", "RepoDigests", "Created", "Architecture", "Os",
    )}
    if not identity["Id"]:
        raise RuntimeError("docker image inspect did not report an image ID")
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "uname": uname._asdict(),
        "lscpu": json.loads(_snapshot_command(["lscpu", "--json"])),
        "snapshot_process_affinity": sorted(os.sched_getaffinity(0)),
        "files": ([_snapshot_file(path, required=True) for path in required_files]
                  + [_snapshot_file(path) for path in optional_files]),
        "kernel_image": _snapshot_file(f"/boot/vmlinuz-{uname.release}", text=False),
        "kernel_btf": _snapshot_file("/sys/kernel/btf/vmlinux", text=False),
        "runtime_image": identity,
        # Inspect the immutable image ID, without host mounts or environment forwarding.
        "image_artifacts": json.loads(_snapshot_command([
            "docker", "run", "--rm", "--entrypoint", image_python,
            str(identity["Id"]), "-c", image_script,
        ])),
    }


def _remote_scripts() -> tuple[str, str]:
    imports = ("import hashlib, json, os, platform, subprocess, sys\n"
               "from datetime import datetime, timezone\nfrom pathlib import Path\n")
    read_file = inspect.getsource(_snapshot_file)
    image_script = imports + read_file + inspect.getsource(_image_snapshot) + "\nprint(json.dumps(_image_snapshot()))\n"
    host_script = (imports + read_file + inspect.getsource(_snapshot_command)
                   + inspect.getsource(_machine_snapshot)
                   + "\nprint(json.dumps(_machine_snapshot(*sys.argv[1:])))\n")
    return host_script, image_script


def capture_snapshot(ctx: aws_common.AwsExecutorContext, state: dict[str, str], path: Path) -> None:
    """Save partial evidence before propagating any mandatory snapshot failure."""
    snapshot = {
        "run_token": ctx.run_token, "target": ctx.target_name, "suite": ctx.suite_name,
        "region": ctx.aws_region, "captured_at": datetime.now(timezone.utc).isoformat(),
        "status": "collecting", "instance_state": dict(state),
    }
    write_json_object(path, snapshot)
    try:
        instance_id = state.get("STATE_INSTANCE_ID", "")
        if not instance_id:
            raise RuntimeError("no measurement instance ID was recorded")
        described = aws_common._aws_cmd(
            ctx, "ec2", "describe-instances", "--instance-ids", instance_id,
            "--query", INSTANCE_QUERY, "--output", "json", capture_output=True,
        )
        if described.returncode:
            raise RuntimeError(f"describe measurement instance failed: {described.stderr.strip()}")
        snapshot["instance"] = json.loads(described.stdout)
        if not isinstance(snapshot["instance"], dict) or snapshot["instance"].get("InstanceId") != instance_id:
            raise RuntimeError("describe-instances returned a missing or mismatched instance ID")
        write_json_object(path, snapshot)
        ip = state.get("STATE_INSTANCE_IP", "")
        if not ip:
            raise RuntimeError("no measurement instance IP was recorded for the host snapshot")
        host_script, image_script = _remote_scripts()
        remote = aws_common._ssh_exec(
            ctx, ip, "sudo", ctx.contract.remote.python_bin or "python3", "-c", host_script,
            ctx.contract.remote.runtime_container_image,
            ctx.contract.remote.runtime_python_bin or "python3", image_script,
            check=False, capture_output=True,
        )
        if remote.returncode:
            raise RuntimeError(f"measurement host snapshot failed: {remote.stderr.strip()}")
        snapshot["machine"] = json.loads(remote.stdout)
        snapshot["status"] = "completed"
        snapshot["completed_at"] = datetime.now(timezone.utc).isoformat()
        write_json_object(path, snapshot)
    except BaseException as exc:
        snapshot["status"] = "error"
        snapshot["error"] = str(exc)
        write_json_object(path, snapshot)
        raise
