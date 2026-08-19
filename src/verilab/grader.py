from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import ProjectPolicy
from .repository import verify_policy_code


class GraderError(RuntimeError):
    pass


@dataclass(frozen=True)
class GraderResult:
    metrics: dict[str, float]
    raw: dict[str, Any]
    output_path: Path


def _strict_metrics(value: object) -> dict[str, float]:
    if not isinstance(value, dict) or not value:
        raise GraderError("grader output metrics must be a non-empty object")
    result: dict[str, float] = {}
    for name, score in value.items():
        if not isinstance(name, str) or not name:
            raise GraderError("grader metric names must be non-empty strings")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise GraderError(f"grader metric {name!r} is not numeric")
        number = float(score)
        if not (-float("inf") < number < float("inf")):
            raise GraderError(f"grader metric {name!r} is not finite")
        result[name] = number
    return result


class TrustedGrader:
    def __init__(self, policy: ProjectPolicy, project_root: Path) -> None:
        self.policy = policy
        self.project_root = project_root.resolve()

    def run(
        self,
        *,
        manifest_path: Path,
        output_path: Path,
        run_dir: Path,
        worktree: Path,
    ) -> GraderResult:
        errors = verify_policy_code(self.policy)
        if errors:
            raise GraderError("; ".join(errors))
        substitutions = {
            "{manifest}": str(manifest_path),
            "{output}": str(output_path),
            "{run_dir}": str(run_dir),
            "{worktree}": str(worktree),
            "{project_root}": str(self.project_root),
        }
        command = []
        for argument in self.policy.grader_command:
            rendered = argument
            for marker, replacement in substitutions.items():
                rendered = rendered.replace(marker, replacement)
            command.append(rendered)
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "PYTHONNOUSERSITE": "1",
            "VERILAB_ARTIFACT_MANIFEST": str(manifest_path),
            "VERILAB_GRADER_OUTPUT": str(output_path),
        }
        completed = subprocess.run(
            command,
            cwd=self.project_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=self.policy.run_timeout_seconds or 3600,
        )
        if completed.returncode != 0:
            raise GraderError(
                f"trusted grader exited {completed.returncode}: {completed.stderr[-2000:]}"
            )
        try:
            raw = json.loads(output_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise GraderError("trusted grader did not produce valid JSON") from exc
        if not isinstance(raw, dict) or set(raw) - {
            "schema_version",
            "metrics",
            "protocol_id",
            "cohort",
            "details",
        }:
            raise GraderError("trusted grader output has unexpected fields")
        if raw.get("schema_version") != 1:
            raise GraderError("trusted grader output schema_version must be 1")
        if raw.get("protocol_id", self.policy.protocol_id) != self.policy.protocol_id:
            raise GraderError("trusted grader protocol does not match policy")
        if raw.get("cohort", self.policy.cohort) != self.policy.cohort:
            raise GraderError("trusted grader cohort does not match policy")
        metrics = _strict_metrics(raw.get("metrics"))
        if self.policy.primary_metric not in metrics:
            raise GraderError(f"primary metric {self.policy.primary_metric!r} is missing")
        return GraderResult(metrics=metrics, raw=raw, output_path=output_path)


def read_reported_metrics(
    artifact_rows: list[dict[str, Any]], policy: ProjectPolicy
) -> dict[str, float]:
    if not policy.reported_metric_role:
        return {}
    matches = [row for row in artifact_rows if row["role"] == policy.reported_metric_role]
    if not matches:
        return {}
    if len(matches) != 1:
        raise GraderError("reported metric role must resolve to exactly one artifact")
    path = Path(matches[0]["absolute_path"])
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise GraderError("reported metrics artifact is not valid JSON") from exc
    if isinstance(value, dict) and "metrics" in value:
        value = value["metrics"]
    return _strict_metrics(value)


def metric_consistent(
    reported: dict[str, float], computed: dict[str, float], policy: ProjectPolicy
) -> bool:
    if policy.primary_metric not in reported:
        return True
    return (
        abs(reported[policy.primary_metric] - computed[policy.primary_metric])
        <= policy.metric_tolerance
    )
