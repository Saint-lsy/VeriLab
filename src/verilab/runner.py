from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .models import ExperimentSpec, ProjectPolicy, canonical_json, ensure_within
from .security import (
    process_cpu_ticks,
    process_start_ticks,
    sanitized_process_environment,
    sha256_file,
)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class RunReceipt:
    exit_code: int
    started_at: str
    finished_at: str
    pid: int
    process_start_ticks: int | None
    command_fingerprint: str
    receipt_path: Path
    receipt_sha256: str


class RunExecutor:
    def __init__(self, heartbeat_seconds: float = 2.0) -> None:
        self.heartbeat_seconds = heartbeat_seconds

    @staticmethod
    def command_fingerprint(command: list[str], cwd: Path, commit: str) -> str:
        material = {"command": command, "cwd": str(cwd), "commit": commit}
        return hashlib.sha256(canonical_json(material).encode()).hexdigest()

    @staticmethod
    def _gpu_sample(gpu_ids: list[str]) -> list[dict[str, str]]:
        if not gpu_ids or not shutil.which("nvidia-smi"):
            return []
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid,utilization.gpu,memory.used,power.draw",
                "--format=csv,noheader,nounits",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
        wanted = set(gpu_ids)
        samples: list[dict[str, str]] = []
        for line in completed.stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) == 5 and parts[0] in wanted:
                samples.append(
                    dict(
                        zip(
                            ("index", "uuid", "utilization_gpu", "memory_used_mib", "power_w"),
                            parts,
                            strict=True,
                        )
                    )
                )
        return samples

    def execute(
        self,
        *,
        spec: ExperimentSpec,
        policy: ProjectPolicy,
        worktree: Path,
        run_dir: Path,
        commit: str,
        secret_values: dict[str, str] | None = None,
        on_started: Callable[[int, int | None, str, str], None] | None = None,
        on_heartbeat: Callable[[int, int | None, list[dict[str, str]], str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> RunReceipt:
        run_dir.mkdir(parents=True, exist_ok=True)
        cwd = ensure_within(worktree, spec.cwd)
        if not cwd.is_dir():
            raise RuntimeError(f"run cwd does not exist in worktree: {cwd}")
        environment = {
            key: value
            for key, value in sanitized_process_environment().items()
            if not key.startswith("VERILAB_") and not key.startswith("CODEX_")
        }
        environment.update(spec.env)
        if spec.resource_claim.gpu_ids:
            environment["CUDA_VISIBLE_DEVICES"] = ",".join(spec.resource_claim.gpu_ids)
        for name in spec.secret_refs:
            if name not in policy.secret_names:
                raise RuntimeError(f"secret reference is not allowed by policy: {name}")
            if not secret_values or name not in secret_values:
                raise RuntimeError(f"secret reference is unavailable: {name}")
            environment[name] = secret_values[name]
        environment["VERILAB_RUN_DIR"] = str(run_dir)
        environment["VERILAB_EXPERIMENT_COMMIT"] = commit
        log_path = run_dir / "run.log"
        started_at = now()
        fingerprint = self.command_fingerprint(spec.command, cwd, commit)
        with log_path.open("ab", buffering=0) as log:
            process = subprocess.Popen(
                spec.command,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            start_ticks = process_start_ticks(process.pid)
            if on_started:
                on_started(process.pid, start_ticks, fingerprint, started_at)
            deadline = (
                time.monotonic() + policy.run_timeout_seconds
                if policy.run_timeout_seconds
                else None
            )
            cancelled = False
            while process.poll() is None:
                if should_cancel and should_cancel():
                    self.terminate_process_group(process.pid)
                    cancelled = True
                if deadline is not None and time.monotonic() >= deadline:
                    self.terminate_process_group(process.pid)
                    cancelled = True
                if on_heartbeat:
                    on_heartbeat(
                        log_path.stat().st_size if log_path.exists() else 0,
                        process_cpu_ticks(process.pid),
                        self._gpu_sample(spec.resource_claim.gpu_ids),
                        now(),
                    )
                if cancelled:
                    break
                time.sleep(self.heartbeat_seconds)
            try:
                exit_code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.kill_process_group(process.pid)
                exit_code = process.wait(timeout=10)
        finished_at = now()
        receipt_path = run_dir / "exit-receipt.json"
        receipt = {
            "schema_version": 1,
            "commit": commit,
            "command": spec.command,
            "command_fingerprint": fingerprint,
            "pid": process.pid,
            "process_start_ticks": start_ticks,
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": exit_code,
            "cancelled": cancelled,
        }
        receipt_path.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return RunReceipt(
            exit_code=exit_code,
            started_at=started_at,
            finished_at=finished_at,
            pid=process.pid,
            process_start_ticks=start_ticks,
            command_fingerprint=fingerprint,
            receipt_path=receipt_path,
            receipt_sha256=sha256_file(receipt_path),
        )

    @staticmethod
    def terminate_process_group(pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    @staticmethod
    def kill_process_group(pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
