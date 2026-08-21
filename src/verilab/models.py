from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class ExperimentStatus(StrEnum):
    DRAFT = "DRAFT"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    GRADING = "GRADING"
    REVIEW_PENDING = "REVIEW_PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    REVIEW_BLOCKED = "REVIEW_BLOCKED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    ORPHANED = "ORPHANED"


TERMINAL_STATUSES = {
    ExperimentStatus.ACCEPTED,
    ExperimentStatus.REJECTED,
    ExperimentStatus.FAILED,
    ExperimentStatus.CANCELLED,
    ExperimentStatus.VERIFICATION_FAILED,
    ExperimentStatus.NEEDS_HUMAN,
    ExperimentStatus.ORPHANED,
}


ALLOWED_TRANSITIONS: dict[ExperimentStatus, set[ExperimentStatus]] = {
    ExperimentStatus.DRAFT: {ExperimentStatus.QUEUED, ExperimentStatus.CANCELLED},
    ExperimentStatus.QUEUED: {
        ExperimentStatus.RUNNING,
        ExperimentStatus.CANCELLED,
        ExperimentStatus.FAILED,
    },
    ExperimentStatus.RUNNING: {
        ExperimentStatus.GRADING,
        ExperimentStatus.CANCELLED,
        ExperimentStatus.FAILED,
        ExperimentStatus.ORPHANED,
    },
    ExperimentStatus.GRADING: {
        ExperimentStatus.REVIEW_PENDING,
        ExperimentStatus.VERIFICATION_FAILED,
        ExperimentStatus.FAILED,
    },
    ExperimentStatus.REVIEW_PENDING: {
        ExperimentStatus.ACCEPTED,
        ExperimentStatus.REJECTED,
        ExperimentStatus.REVIEW_BLOCKED,
        ExperimentStatus.NEEDS_HUMAN,
        ExperimentStatus.VERIFICATION_FAILED,
    },
    ExperimentStatus.REVIEW_BLOCKED: {ExperimentStatus.REVIEW_PENDING},
    ExperimentStatus.NEEDS_HUMAN: {ExperimentStatus.REVIEW_PENDING},
    ExperimentStatus.ACCEPTED: set(),
    ExperimentStatus.REJECTED: set(),
    ExperimentStatus.FAILED: set(),
    ExperimentStatus.CANCELLED: set(),
    ExperimentStatus.VERIFICATION_FAILED: set(),
    ExperimentStatus.ORPHANED: set(),
}


class ArtifactExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.-]+$")
    glob: str = Field(min_length=1, max_length=512)
    required: bool = True

    @field_validator("glob")
    @classmethod
    def safe_glob(cls, value: str) -> str:
        path = PurePosixPath(value.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("artifact glob must stay inside VERILAB_RUN_DIR")
        return value


class ResourceClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gpu_ids: list[str] = Field(default_factory=list)
    cpu_cores: int = Field(default=1, ge=1)
    memory_gib: float = Field(default=1, gt=0)


class ExperimentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    title: str = Field(min_length=1, max_length=240)
    hypothesis: str = Field(min_length=1, max_length=8000)
    parent_experiment_id: str | None = None
    git_commit: str = Field(min_length=7, max_length=64, pattern=r"^[0-9a-fA-F]+$")
    command: list[str] = Field(min_length=1, max_length=256)
    cwd: str = "."
    env: dict[str, str] = Field(default_factory=dict)
    secret_refs: list[str] = Field(default_factory=list)
    protocol_id: str = Field(min_length=1, max_length=120)
    expected_artifacts: list[ArtifactExpectation] = Field(min_length=1)
    resource_claim: ResourceClaim = Field(default_factory=ResourceClaim)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("command")
    @classmethod
    def argv_only(cls, value: list[str]) -> list[str]:
        if any(not isinstance(item, str) or not item or "\x00" in item for item in value):
            raise ValueError("command must be a non-empty argv array")
        return value

    @field_validator("cwd")
    @classmethod
    def safe_cwd(cls, value: str) -> str:
        path = PurePosixPath(value.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("cwd must stay inside the detached worktree")
        return value

    @field_validator("env")
    @classmethod
    def safe_env(cls, value: dict[str, str]) -> dict[str, str]:
        denied = {"VERILAB_CAPABILITY", "VERILAB_STATE_DIR", "VERILAB_POLICY_PATH"}
        overlap = denied.intersection(value)
        if overlap:
            raise ValueError(f"reserved environment variables: {sorted(overlap)}")
        for key, item in value.items():
            if not key or "=" in key or "\x00" in key or "\x00" in item:
                raise ValueError("invalid environment entry")
            upper = key.upper()
            if any(marker in upper for marker in ("TOKEN", "PASSWORD", "SECRET", "API_KEY")):
                raise ValueError(f"secret-like environment key must use secret_refs: {key}")
        return value

    @model_validator(mode="after")
    def unique_artifact_roles(self) -> ExperimentSpec:
        roles = [item.role for item in self.expected_artifacts]
        if len(roles) != len(set(roles)):
            raise ValueError("expected artifact roles must be unique")
        visible = self.env.get("CUDA_VISIBLE_DEVICES")
        if visible is not None:
            declared = [item.strip() for item in visible.split(",") if item.strip()]
            if declared != self.resource_claim.gpu_ids:
                raise ValueError("CUDA_VISIBLE_DEVICES must match resource_claim.gpu_ids")
        return self

    @property
    def spec_hash(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class ProjectPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    project_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", min_length=1, max_length=120)
    protocol_id: str = Field(min_length=1, max_length=120)
    grader_command: list[str] = Field(min_length=1)
    grader_code_paths: list[str] = Field(default_factory=list)
    primary_metric: str = Field(min_length=1, max_length=120)
    direction: Literal["maximize", "minimize"] = "maximize"
    dataset: str = Field(min_length=1)
    split: str = Field(min_length=1)
    cohort: str = Field(min_length=1)
    preprocessing: str = Field(min_length=1)
    evaluator: str = Field(min_length=1)
    required_artifact_roles: list[str] = Field(min_length=1)
    reported_metric_role: str | None = "reported_metrics"
    metric_tolerance: float = Field(default=1e-9, ge=0)
    small_artifact_limit_mib: int = Field(default=64, ge=1, le=4096)
    reviewer_timeout_seconds: int = Field(default=900, ge=1)
    run_timeout_seconds: int | None = Field(default=None, ge=1)
    secret_names: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("grader_command")
    @classmethod
    def grader_argv_only(cls, value: list[str]) -> list[str]:
        if any(not isinstance(item, str) or not item or "\x00" in item for item in value):
            raise ValueError("grader_command must be an argv array")
        return value

    @property
    def policy_hash(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))

    @property
    def comparison_key(self) -> str:
        return canonical_sha256(
            {
                "policy_hash": self.policy_hash,
                "protocol_id": self.protocol_id,
                "dataset": self.dataset,
                "split": self.split,
                "cohort": self.cohort,
                "preprocessing": self.preprocessing,
                "metric": self.primary_metric,
                "direction": self.direction,
                "evaluator": self.evaluator,
            }
        )


class ReviewCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: Literal["pass", "fail", "unknown"]
    evidence_refs: list[str]
    note: str


class ChangeSummary(BaseModel):
    """Human-facing explanation of what changed and what the result means."""

    model_config = ConfigDict(extra="forbid")

    headline: str = Field(min_length=1, max_length=240)
    summary: str = Field(min_length=1, max_length=2000)
    key_changes: list[str] = Field(min_length=1, max_length=8)
    expected_effect: str = Field(min_length=1, max_length=1000)
    observed_effect: str = Field(min_length=1, max_length=1000)
    evidence_refs: list[str] = Field(min_length=1, max_length=20)

    @field_validator("headline", "summary", "expected_effect", "observed_effect")
    @classmethod
    def narrative_text_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("narrative text must not be blank")
        return value

    @field_validator("key_changes")
    @classmethod
    def key_changes_must_not_be_blank(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("key changes must not contain blank items")
        return cleaned


class ReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: Literal["eligible", "ineligible", "needs_human"]
    checks: list[ReviewCheck]
    summary: str
    change_summary: ChangeSummary


REQUIRED_REVIEW_CHECKS = {
    "authorized_execution",
    "execution_evidence",
    "artifact_integrity",
    "metric_reproducibility",
    "protocol_compliance",
    "data_and_split_integrity",
    "result_consistency",
    "required_artifacts_complete",
}


def validate_review_semantics(
    review: ReviewOutput,
    *,
    bundle_sha256: str,
    allowed_event_refs: set[str],
    allowed_sha_refs: set[str],
) -> tuple[bool, str]:
    if review.target_bundle_sha256 != bundle_sha256:
        return False, "reviewer bundle hash does not match"
    names = [check.name for check in review.checks]
    if set(names) != REQUIRED_REVIEW_CHECKS or len(names) != len(REQUIRED_REVIEW_CHECKS):
        return False, "reviewer checks are missing, duplicated, or unexpected"
    allowed = allowed_event_refs | allowed_sha_refs
    for check in review.checks:
        if check.status != "pass":
            return False, f"review check {check.name} is {check.status}"
        if not check.evidence_refs or any(ref not in allowed for ref in check.evidence_refs):
            return False, f"review check {check.name} has invalid evidence references"
    if any(ref not in allowed for ref in review.change_summary.evidence_refs):
        return False, "change summary has invalid evidence references"
    if review.verdict != "eligible":
        return False, f"review verdict is {review.verdict}"
    return True, "eligible"


def ensure_within(root: Path, relative: str) -> Path:
    root = root.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"path escapes trusted root: {relative}")
    return candidate
