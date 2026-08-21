from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from verilab.config import Settings
from verilab.models import REQUIRED_REVIEW_CHECKS, ExperimentSpec, ProjectPolicy, ReviewOutput
from verilab.repository import pin_policy_code
from verilab.service import VeriLabService

EXAMPLE = Path(__file__).parents[1] / "examples" / "dummy-project"


def run_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def commit_all(root: Path, message: str = "test commit") -> str:
    run_git(root, "add", ".")
    run_git(root, "commit", "-m", message)
    return run_git(root, "rev-parse", "HEAD")


class FakeReviewer:
    def __init__(
        self,
        *,
        verdict: str = "eligible",
        check_status: str = "pass",
        missing_check: bool = False,
        bad_bundle: bool = False,
        bad_reference: bool = False,
        raises: Exception | None = None,
    ) -> None:
        self.verdict = verdict
        self.check_status = check_status
        self.missing_check = missing_check
        self.bad_bundle = bad_bundle
        self.bad_reference = bad_reference
        self.raises = raises
        self.calls = 0

    def review(
        self, *, prompt: str, cwd: Path, bundle_dir: Path, timeout: int
    ) -> tuple[ReviewOutput, str | None]:
        del prompt, cwd, timeout
        self.calls += 1
        if self.raises:
            raise self.raises
        bundle_sha = (bundle_dir / "bundle.sha256").read_text(encoding="utf-8").strip()
        events = [
            json.loads(line)
            for line in (bundle_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        reference = f"event:{events[0]['seq']}"
        if self.bad_reference:
            reference = "event:999999999"
        names = sorted(REQUIRED_REVIEW_CHECKS)
        if self.missing_check:
            names.pop()
        output = ReviewOutput.model_validate(
            {
                "target_bundle_sha256": "f" * 64 if self.bad_bundle else bundle_sha,
                "verdict": self.verdict,
                "checks": [
                    {
                        "name": name,
                        "status": self.check_status,
                        "evidence_refs": [reference],
                        "note": "checked against canonical test evidence",
                    }
                    for name in names
                ],
                "summary": "deterministic independent test review",
                "change_summary": {
                    "headline": "Deterministic candidate compared with its declared parent",
                    "summary": (
                        "The candidate keeps the test protocol fixed and records its declared "
                        "configuration as a human-readable audit statement."
                    ),
                    "key_changes": [
                        "The frozen experiment specification identifies the candidate."
                    ],
                    "expected_effect": (
                        "The declared change is expected to preserve reproducibility."
                    ),
                    "observed_effect": "The trusted grader computed the recorded test metric.",
                    "evidence_refs": [reference],
                },
            }
        )
        return output, f"fake-thread-{self.calls}"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    shutil.copytree(EXAMPLE, root)
    run_git(root, "init", "-b", "main")
    run_git(root, "config", "user.email", "verilab@example.test")
    run_git(root, "config", "user.name", "VeriLab Tests")
    commit_all(root, "dummy baseline")
    return root


def load_policy(root: Path, **updates: Any) -> ProjectPolicy:
    policy = ProjectPolicy.model_validate_json((root / "policy.json").read_text(encoding="utf-8"))
    if updates:
        policy = policy.model_copy(update=updates)
    return pin_policy_code(policy, root)


@pytest.fixture
def service_factory(project: Path, tmp_path: Path):
    counter = 0

    def factory(
        reviewer: FakeReviewer | None = None,
        *,
        policy: ProjectPolicy | None = None,
        state_name: str | None = None,
    ) -> VeriLabService:
        nonlocal counter
        counter += 1
        state = tmp_path / (state_name or f"state-{counter}")
        settings = Settings.load(project_root=project, state_dir=state)
        settings = replace(settings, heartbeat_seconds=0.01)
        settings.install_policy(policy or load_policy(project))
        return VeriLabService(settings, reviewer=reviewer or FakeReviewer())

    return factory


def make_spec(
    project: Path,
    *,
    title: str = "Dummy baseline",
    command: list[str] | None = None,
    expected_artifacts: list[dict[str, Any]] | None = None,
    parent_experiment_id: str | None = None,
) -> ExperimentSpec:
    commit = run_git(project, "rev-parse", "HEAD")
    return ExperimentSpec.model_validate(
        {
            "schema_version": 1,
            "title": title,
            "hypothesis": "This completed run should remain auditable regardless of its score.",
            "parent_experiment_id": parent_experiment_id,
            "git_commit": commit,
            "command": command
            or [
                "python3",
                "train.py",
                "--predictions",
                "0,1,0,0",
                "--reported-score",
                "0.75",
            ],
            "cwd": ".",
            "env": {},
            "secret_refs": [],
            "protocol_id": "public-oof-v1",
            "expected_artifacts": expected_artifacts
            or [
                {"role": "predictions", "glob": "outputs/predictions.json", "required": True},
                {
                    "role": "reported_metrics",
                    "glob": "outputs/reported_metrics.json",
                    "required": True,
                },
                {"role": "checkpoint", "glob": "outputs/checkpoint.bin", "required": False},
            ],
            "resource_claim": {"gpu_ids": [], "cpu_cores": 1, "memory_gib": 1},
            "metadata": {"test": True},
        }
    )
