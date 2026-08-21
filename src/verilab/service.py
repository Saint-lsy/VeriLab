from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from .artifacts import ArtifactError, ArtifactStore, SealedArtifact
from .bundle import AuditBundle, reviewer_prompt
from .codex_driver import CodexDriver, CodexDriverError
from .config import Settings
from .db import Database
from .grader import (
    GraderError,
    TrustedGrader,
    metric_consistent,
    read_reported_metrics,
)
from .ledger import EventLedger, utc_now
from .models import (
    ALLOWED_TRANSITIONS,
    REQUIRED_REVIEW_CHECKS,
    ChangeSummary,
    ExperimentSpec,
    ExperimentStatus,
    ProjectPolicy,
    ReviewOutput,
    canonical_sha256,
)
from .repository import (
    RepositoryError,
    create_detached_worktree,
    policy_public_view,
    validate_submission_repository,
)
from .repository import git as repository_git
from .runner import RunExecutor
from .security import (
    redacted_environment,
    same_process,
    sanitized_process_environment,
    sha256_file,
)


class ServiceError(RuntimeError):
    pass


class NotFound(ServiceError):
    pass


class InvalidState(ServiceError):
    pass


class ReviewerBackend(Protocol):
    def review(
        self, *, prompt: str, cwd: Path, bundle_dir: Path, timeout: int
    ) -> tuple[ReviewOutput, str | None]: ...


class CodexReviewerBackend:
    def __init__(self, driver: CodexDriver) -> None:
        self.driver = driver

    def review(
        self, *, prompt: str, cwd: Path, bundle_dir: Path, timeout: int
    ) -> tuple[ReviewOutput, str | None]:
        output, result = self.driver.reviewer(
            prompt=prompt,
            cwd=cwd,
            bundle_dir=bundle_dir,
            timeout=timeout,
        )
        return output, result.thread_id


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class VeriLabService:
    def __init__(
        self,
        settings: Settings,
        *,
        reviewer: ReviewerBackend | None = None,
        runner: RunExecutor | None = None,
    ) -> None:
        self.settings = settings
        self.settings.ensure_layout()
        self.db = Database(settings.database_path)
        self.db.migrate()
        self.ledger = EventLedger(self.db)
        self.artifact_store = ArtifactStore(settings.state_dir)
        self.runner = runner or RunExecutor(settings.heartbeat_seconds)
        self.codex_driver = CodexDriver(settings.codex_binary)
        self.reviewer = reviewer or CodexReviewerBackend(self.codex_driver)
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._pipeline_lock = threading.Lock()
        self._recovery_monitors: set[str] = set()

    @property
    def policy(self) -> ProjectPolicy:
        return self.settings.load_policy()

    def _policy_for_experiment(self, experiment: str | dict[str, Any]) -> ProjectPolicy:
        row = self._experiment_row(experiment) if isinstance(experiment, str) else experiment
        return self.settings.load_policy(row["policy_hash"])

    def start(self) -> None:
        self.recover()
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._worker_loop, name="verilab-worker", daemon=True
        )
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=10)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_next()
            except Exception as exc:  # a daemon must leave durable failure evidence
                self.ledger.append(
                    entity_type="controller",
                    entity_id=self.policy.project_id,
                    event_type="controller.worker_error",
                    payload={"error": type(exc).__name__, "message": str(exc)},
                )
                worked = False
            self._stop.wait(0.25 if worked else 1.0)

    def submit(self, spec: ExperimentSpec | dict[str, Any]) -> dict[str, Any]:
        if not isinstance(spec, ExperimentSpec):
            spec = ExperimentSpec.model_validate(spec)
        policy = self.policy
        self.settings.load_policy(policy.policy_hash)
        if spec.protocol_id != policy.protocol_id:
            raise ServiceError(f"spec protocol {spec.protocol_id!r} does not match trusted policy")
        commit = validate_submission_repository(self.settings.project_root, spec)
        frozen = spec.model_copy(update={"git_commit": commit})
        now = utc_now()
        with self.db.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM experiments WHERE policy_hash = ? AND spec_hash = ?",
                (policy.policy_hash, frozen.spec_hash),
            ).fetchone()
            if existing:
                run = connection.execute(
                    "SELECT * FROM runs WHERE experiment_id = ?", (existing["id"],)
                ).fetchone()
                return {
                    "experiment_id": existing["id"],
                    "run_id": run["id"],
                    "status": existing["status"],
                    "deduplicated": True,
                }
            if frozen.parent_experiment_id:
                parent = connection.execute(
                    "SELECT comparison_key FROM experiments WHERE id = ?",
                    (frozen.parent_experiment_id,),
                ).fetchone()
                if not parent:
                    raise ServiceError("parent experiment does not exist")
                if parent["comparison_key"] != policy.comparison_key:
                    raise ServiceError("parent experiment belongs to another comparison key")
            experiment_id = _id("exp")
            run_id = _id("run")
            ticket_hash = canonical_sha256(
                {
                    "experiment_id": experiment_id,
                    "run_id": run_id,
                    "spec_hash": frozen.spec_hash,
                    "policy_hash": policy.policy_hash,
                    "commit": commit,
                }
            )
            run_dir = self.settings.state_dir / "runs" / run_id
            worktree = self.settings.state_dir / "worktrees" / run_id
            connection.execute(
                """
                INSERT INTO experiments(
                    id, spec_hash, spec_json, title, hypothesis, parent_experiment_id,
                    git_commit, protocol_id, policy_hash, comparison_key, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    frozen.spec_hash,
                    frozen.model_dump_json(),
                    frozen.title,
                    frozen.hypothesis,
                    frozen.parent_experiment_id,
                    commit,
                    frozen.protocol_id,
                    policy.policy_hash,
                    policy.comparison_key,
                    ExperimentStatus.QUEUED,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO runs(
                    id, experiment_id, ticket_hash, status, command_json,
                    run_dir, worktree_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    experiment_id,
                    ticket_hash,
                    ExperimentStatus.QUEUED,
                    json.dumps(frozen.command),
                    str(run_dir),
                    str(worktree),
                ),
            )
            self.ledger.append(
                entity_type="experiment",
                entity_id=experiment_id,
                event_type="experiment.submitted",
                actor="executor",
                payload={
                    "spec_hash": frozen.spec_hash,
                    "policy_hash": policy.policy_hash,
                    "comparison_key": policy.comparison_key,
                    "git_commit": commit,
                    "run_id": run_id,
                    "ticket_hash": ticket_hash,
                },
                connection=connection,
            )
        return {
            "experiment_id": experiment_id,
            "run_id": run_id,
            "status": ExperimentStatus.QUEUED,
            "deduplicated": False,
        }

    def _transition(
        self,
        experiment_id: str,
        target: ExperimentStatus,
        *,
        reason: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self.db.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT status FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
            if not row:
                raise NotFound(experiment_id)
            current = ExperimentStatus(row["status"])
            if target == current:
                return
            if target not in ALLOWED_TRANSITIONS[current]:
                raise InvalidState(f"cannot transition {current} to {target}")
            connection.execute(
                "UPDATE experiments SET status = ?, updated_at = ? WHERE id = ?",
                (target, utc_now(), experiment_id),
            )
            self.ledger.append(
                entity_type="experiment",
                entity_id=experiment_id,
                event_type="experiment.status_changed",
                payload={
                    "from": current,
                    "to": target,
                    "reason": reason,
                    **(payload or {}),
                },
                connection=connection,
            )

    def _has_active_pipeline(self) -> bool:
        active = (
            ExperimentStatus.RUNNING,
            ExperimentStatus.GRADING,
            ExperimentStatus.REVIEW_PENDING,
        )
        placeholders = ",".join("?" for _ in active)
        with self.db.connect() as connection:
            row = connection.execute(
                f"SELECT 1 FROM experiments WHERE status IN ({placeholders}) LIMIT 1", active
            ).fetchone()
        return bool(row)

    def run_next(self) -> bool:
        if not self._pipeline_lock.acquire(blocking=False):
            return False
        try:
            if self._has_active_pipeline():
                return False
            with self.db.connect() as connection:
                row = connection.execute(
                    """
                    SELECT e.*, r.id AS run_id, r.run_dir, r.worktree_path
                    FROM experiments e JOIN runs r ON r.experiment_id = e.id
                    WHERE e.status = ? ORDER BY e.created_at, e.id LIMIT 1
                    """,
                    (ExperimentStatus.QUEUED,),
                ).fetchone()
            if not row:
                return False
            self._run_experiment(dict(row))
            return True
        finally:
            self._pipeline_lock.release()

    def _run_experiment(self, experiment: dict[str, Any]) -> None:
        experiment_id = experiment["id"]
        run_id = experiment["run_id"]
        spec = ExperimentSpec.model_validate_json(experiment["spec_json"])
        policy = self._policy_for_experiment(experiment)
        run_dir = Path(experiment["run_dir"])
        worktree = Path(experiment["worktree_path"])
        try:
            create_detached_worktree(self.settings.project_root, worktree, experiment["git_commit"])
            self._transition(experiment_id, ExperimentStatus.RUNNING, reason="run ticket started")
            with self.db.transaction(immediate=True) as connection:
                connection.execute("UPDATE runs SET status = ? WHERE id = ?", ("RUNNING", run_id))
                self.ledger.append(
                    entity_type="run",
                    entity_id=run_id,
                    event_type="run.authorized",
                    payload={
                        "ticket_hash": self._run_row(run_id)["ticket_hash"],
                        "commit": experiment["git_commit"],
                        "command": spec.command,
                        "cwd": spec.cwd,
                        "resource_claim": spec.resource_claim.model_dump(mode="json"),
                    },
                    connection=connection,
                )

            def on_started(pid: int, ticks: int | None, fingerprint: str, started: str) -> None:
                with self.db.transaction(immediate=True) as connection:
                    connection.execute(
                        """
                        UPDATE runs SET pid = ?, process_start_ticks = ?, command_fingerprint = ?,
                            started_at = ?, heartbeat_at = ? WHERE id = ?
                        """,
                        (pid, ticks, fingerprint, started, started, run_id),
                    )
                    self.ledger.append(
                        entity_type="run",
                        entity_id=run_id,
                        event_type="run.process_started",
                        payload={
                            "pid": pid,
                            "process_start_ticks": ticks,
                            "command_fingerprint": fingerprint,
                            "started_at": started,
                        },
                        connection=connection,
                    )

            def on_heartbeat(
                log_size: int,
                cpu_ticks: int | None,
                gpu: list[dict[str, str]],
                at: str,
            ) -> None:
                with self.db.transaction(immediate=True) as connection:
                    connection.execute(
                        """
                        UPDATE runs SET heartbeat_at = ?, stdout_size = ?, cpu_ticks = ?,
                            gpu_sample_json = ? WHERE id = ?
                        """,
                        (at, log_size, cpu_ticks, json.dumps(gpu), run_id),
                    )
                    self.ledger.append(
                        entity_type="run",
                        entity_id=run_id,
                        event_type="run.heartbeat",
                        payload={
                            "at": at,
                            "log_size": log_size,
                            "cpu_ticks": cpu_ticks,
                            "gpu": gpu,
                        },
                        connection=connection,
                    )

            receipt = self.runner.execute(
                spec=spec,
                policy=policy,
                worktree=worktree,
                run_dir=run_dir,
                commit=experiment["git_commit"],
                secret_values={
                    name: os.environ[name] for name in spec.secret_refs if name in os.environ
                },
                on_started=on_started,
                on_heartbeat=on_heartbeat,
                should_cancel=lambda: bool(self._run_row(run_id)["cancel_requested"]),
            )
            with self.db.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    UPDATE runs SET status = ?, finished_at = ?, exit_code = ?,
                        exit_receipt_sha256 = ? WHERE id = ?
                    """,
                    (
                        "FINISHED",
                        receipt.finished_at,
                        receipt.exit_code,
                        receipt.receipt_sha256,
                        run_id,
                    ),
                )
                self.ledger.append(
                    entity_type="run",
                    entity_id=run_id,
                    event_type="run.finished",
                    payload={
                        "exit_code": receipt.exit_code,
                        "receipt_sha256": receipt.receipt_sha256,
                        "finished_at": receipt.finished_at,
                    },
                    connection=connection,
                )
            execution_evidence = [
                self.artifact_store.seal_file(
                    run_dir=run_dir,
                    path=run_dir / "run.log",
                    role="execution_log",
                    required=True,
                    limit_bytes=policy.small_artifact_limit_mib * 1024 * 1024,
                ),
                self.artifact_store.seal_file(
                    run_dir=run_dir,
                    path=receipt.receipt_path,
                    role="exit_receipt",
                    required=True,
                    limit_bytes=policy.small_artifact_limit_mib * 1024 * 1024,
                ),
            ]
            self._insert_artifacts(experiment_id, run_id, execution_evidence)
            if self._run_row(run_id)["cancel_requested"]:
                self._transition(
                    experiment_id,
                    ExperimentStatus.CANCELLED,
                    reason="cancel request terminated run",
                )
                return
            if receipt.exit_code != 0:
                self._transition(
                    experiment_id,
                    ExperimentStatus.FAILED,
                    reason=f"formal command exited {receipt.exit_code}",
                )
                return
            self._transition(
                experiment_id, ExperimentStatus.GRADING, reason="formal command succeeded"
            )
            self._grade(experiment_id)
        except (RepositoryError, OSError, RuntimeError) as exc:
            with self.db.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                    ("FAILED", str(exc), utc_now(), run_id),
                )
            current = self._experiment_row(experiment_id)["status"]
            if current in (ExperimentStatus.QUEUED, ExperimentStatus.RUNNING):
                self._transition(
                    experiment_id,
                    ExperimentStatus.FAILED,
                    reason="controller could not execute formal run",
                    payload={"error": type(exc).__name__, "message": str(exc)},
                )

    def _insert_artifacts(
        self, experiment_id: str, run_id: str, artifacts: list[SealedArtifact]
    ) -> None:
        with self.db.transaction(immediate=True) as connection:
            for artifact in artifacts:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO artifacts(
                        id, experiment_id, run_id, role, relative_path, absolute_path,
                        sha256, size_bytes, required, object_path, sealed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.id,
                        experiment_id,
                        run_id,
                        artifact.role,
                        artifact.relative_path,
                        artifact.absolute_path,
                        artifact.sha256,
                        artifact.size_bytes,
                        int(artifact.required),
                        artifact.object_path,
                        utc_now(),
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                self.ledger.append(
                    entity_type="artifact",
                    entity_id=artifact.id,
                    event_type="artifact.sealed",
                    payload={
                        "experiment_id": experiment_id,
                        "run_id": run_id,
                        "role": artifact.role,
                        "relative_path": artifact.relative_path,
                        "sha256": artifact.sha256,
                        "size_bytes": artifact.size_bytes,
                        "required": artifact.required,
                        "copied_to_object_store": bool(artifact.object_path),
                    },
                    connection=connection,
                )

    def _grade(self, experiment_id: str) -> None:
        experiment = self._experiment_row(experiment_id)
        run = self._run_for_experiment(experiment_id)
        spec = ExperimentSpec.model_validate_json(experiment["spec_json"])
        policy = self._policy_for_experiment(experiment)
        run_dir = Path(run["run_dir"])
        worktree = Path(run["worktree_path"])
        try:
            artifacts = self._artifact_rows(experiment_id)
            declared_roles = {item.role for item in spec.expected_artifacts}
            sealed_roles = {item["role"] for item in artifacts}
            if not declared_roles.issubset(sealed_roles):
                sealed = self.artifact_store.seal(run_dir=run_dir, spec=spec, policy=policy)
                self._insert_artifacts(experiment_id, run["id"], sealed)
                artifacts = self._artifact_rows(experiment_id)
            reported = read_reported_metrics(artifacts, policy)
            computed_rows = self._metric_rows(experiment_id, source="computed")
            if computed_rows:
                computed = {row["name"]: row["value"] for row in computed_rows}
            else:
                manifest_path = run_dir / "sealed-artifacts.json"
                manifest_path.write_text(
                    json.dumps(
                        {"schema_version": 1, "artifacts": artifacts},
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                output_path = run_dir / "grader-output.json"
                result = TrustedGrader(policy, self.settings.project_root).run(
                    manifest_path=manifest_path,
                    output_path=output_path,
                    run_dir=run_dir,
                    worktree=worktree,
                )
                computed = result.metrics
                extra = [
                    self.artifact_store.seal_file(
                        run_dir=run_dir,
                        path=manifest_path,
                        role="sealed_manifest",
                        required=True,
                        limit_bytes=policy.small_artifact_limit_mib * 1024 * 1024,
                    ),
                    self.artifact_store.seal_file(
                        run_dir=run_dir,
                        path=output_path,
                        role="grader_output",
                        required=True,
                        limit_bytes=policy.small_artifact_limit_mib * 1024 * 1024,
                    ),
                ]
                self._insert_artifacts(experiment_id, run["id"], extra)
                with self.db.transaction(immediate=True) as connection:
                    for name, value in reported.items():
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO metrics(
                                experiment_id, run_id, name, value, source,
                                comparison_key, created_at
                            ) VALUES (?, ?, ?, ?, 'reported', ?, ?)
                            """,
                            (
                                experiment_id,
                                run["id"],
                                name,
                                value,
                                policy.comparison_key,
                                utc_now(),
                            ),
                        )
                    for name, value in computed.items():
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO metrics(
                                experiment_id, run_id, name, value, source,
                                comparison_key, created_at
                            ) VALUES (?, ?, ?, ?, 'computed', ?, ?)
                            """,
                            (
                                experiment_id,
                                run["id"],
                                name,
                                value,
                                policy.comparison_key,
                                utc_now(),
                            ),
                        )
                    self.ledger.append(
                        entity_type="experiment",
                        entity_id=experiment_id,
                        event_type="grader.completed",
                        payload={
                            "metrics": computed,
                            "comparison_key": policy.comparison_key,
                            "grader_output_sha256": sha256_file(output_path),
                        },
                        actor="trusted-grader",
                        connection=connection,
                    )
            if not metric_consistent(reported, computed, policy):
                self._transition(
                    experiment_id,
                    ExperimentStatus.VERIFICATION_FAILED,
                    reason="reported and independently computed primary metrics disagree",
                    payload={"reported": reported, "computed": computed},
                )
                return
            self._transition(
                experiment_id,
                ExperimentStatus.REVIEW_PENDING,
                reason="trusted grader completed and metrics are consistent",
            )
            self._review(experiment_id)
        except (ArtifactError, GraderError, OSError, ValidationError) as exc:
            current = ExperimentStatus(self._experiment_row(experiment_id)["status"])
            if current == ExperimentStatus.GRADING:
                self._transition(
                    experiment_id,
                    ExperimentStatus.VERIFICATION_FAILED,
                    reason="artifact sealing or trusted grading failed",
                    payload={"error": type(exc).__name__, "message": str(exc)},
                )

    def _pre_review_verify(self, experiment_id: str) -> tuple[bool, list[dict[str, str]]]:
        results: list[dict[str, str]] = []
        okay = True
        for artifact in self._artifact_rows(experiment_id):
            valid, health = self.artifact_store.verify_record(artifact, pre_review=True)
            results.append({"artifact_id": artifact["id"], "health": health})
            if artifact["required"] and not valid:
                okay = False
        return okay, results

    def _review_change_context(
        self,
        experiment: dict[str, Any],
        policy: ProjectPolicy,
        metrics: list[dict[str, Any]],
    ) -> dict[str, Any]:
        current_spec = self._public_spec(experiment["spec_json"], policy)
        current_metric = next(
            (
                row
                for row in metrics
                if row["name"] == policy.primary_metric and row["source"] == "computed"
            ),
            None,
        )
        context: dict[str, Any] = {
            "schema_version": 1,
            "primary_metric": policy.primary_metric,
            "direction": policy.direction,
            "current": {
                "experiment_id": experiment["id"],
                "title": experiment["title"],
                "hypothesis": experiment["hypothesis"],
                "git_commit": experiment["git_commit"],
                "computed_score": current_metric["value"] if current_metric else None,
            },
            "parent": None,
            "score_delta": None,
            "spec_changes": [],
            "git_change_count": 0,
            "git_changes": [],
        }
        parent_id = experiment["parent_experiment_id"]
        if not parent_id:
            return context

        parent = self._experiment_row(parent_id)
        parent_policy = self._policy_for_experiment(parent)
        parent_spec = self._public_spec(parent["spec_json"], parent_policy)
        parent_metrics = self._metric_rows(parent_id)
        parent_metric = next(
            (
                row
                for source in ("verified", "computed")
                for row in parent_metrics
                if row["name"] == policy.primary_metric and row["source"] == source
            ),
            None,
        )
        excluded_fields = {
            "git_commit",
            "parent_experiment_id",
            "schema_version",
            "secret_refs",
            "title",
        }
        spec_changes = [
            {
                "field": field,
                "parent": parent_spec.get(field),
                "current": current_spec.get(field),
            }
            for field in sorted(set(parent_spec) | set(current_spec))
            if field not in excluded_fields and parent_spec.get(field) != current_spec.get(field)
        ]
        git_change_count, git_changes = self._git_change_summary(
            parent["git_commit"], experiment["git_commit"]
        )
        parent_score = parent_metric["value"] if parent_metric else None
        current_score = current_metric["value"] if current_metric else None
        context.update(
            {
                "parent": {
                    "experiment_id": parent["id"],
                    "title": parent["title"],
                    "hypothesis": parent["hypothesis"],
                    "git_commit": parent["git_commit"],
                    "trusted_score": parent_score,
                },
                "score_delta": (
                    current_score - parent_score
                    if current_score is not None and parent_score is not None
                    else None
                ),
                "spec_changes": spec_changes,
                "git_change_count": git_change_count,
                "git_changes": git_changes,
            }
        )
        return context

    def _review(self, experiment_id: str) -> None:
        okay, verification = self._pre_review_verify(experiment_id)
        if not okay:
            self._transition(
                experiment_id,
                ExperimentStatus.VERIFICATION_FAILED,
                reason="sealed artifacts changed before review",
                payload={"artifacts": verification},
            )
            return
        experiment = self._experiment_row(experiment_id)
        run = self._run_for_experiment(experiment_id)
        artifacts = self._artifact_rows(experiment_id)
        metrics = self._metric_rows(experiment_id)
        events = [
            event
            for event in self.ledger.list(after=0, limit=100000)
            if event["entity_id"] in {experiment_id, run["id"]}
            or event["payload"].get("experiment_id") == experiment_id
        ]
        with self.db.connect() as connection:
            attempt = int(
                connection.execute(
                    "SELECT COALESCE(MAX(attempt), 0) + 1 FROM reviews WHERE experiment_id = ?",
                    (experiment_id,),
                ).fetchone()[0]
            )
        review_id = _id("rev")
        reviewer_session_id = _id("session")
        bundle_dir = self.settings.state_dir / "reviewer-bundles" / review_id
        public_experiment = dict(experiment)
        policy = self._policy_for_experiment(experiment)
        public_experiment["spec"] = self._public_spec(public_experiment.pop("spec_json"), policy)
        public_run = dict(run)
        builder = AuditBundle(bundle_dir)
        bundle_sha, event_refs, sha_refs = builder.build(
            experiment=public_experiment,
            run=public_run,
            policy=policy_public_view(policy),
            change_context=self._review_change_context(experiment, policy, metrics),
            artifacts=artifacts,
            metrics=metrics,
            events=events,
        )
        with self.db.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO reviews(
                    id, experiment_id, attempt, bundle_sha256, status, started_at
                ) VALUES (?, ?, ?, ?, 'RUNNING', ?)
                """,
                (review_id, experiment_id, attempt, bundle_sha, utc_now()),
            )
            session_now = utc_now()
            connection.execute(
                """
                INSERT INTO codex_sessions(
                    id, role, experiment_id, status, created_at, updated_at
                ) VALUES (?, 'reviewer', ?, 'RUNNING', ?, ?)
                """,
                (reviewer_session_id, experiment_id, session_now, session_now),
            )
            self.ledger.append(
                entity_type="review",
                entity_id=review_id,
                event_type="review.started",
                payload={
                    "experiment_id": experiment_id,
                    "attempt": attempt,
                    "bundle_sha256": bundle_sha,
                    "sandbox": "read-only",
                    "fresh_thread": True,
                },
                actor="controller",
                connection=connection,
            )
        try:
            output, thread_id = self.reviewer.review(
                prompt=reviewer_prompt(bundle_dir, bundle_sha),
                cwd=Path(run["worktree_path"]),
                bundle_dir=bundle_dir,
                timeout=policy.reviewer_timeout_seconds,
            )
        except (CodexDriverError, TimeoutError, OSError, ValidationError) as exc:
            with self.db.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    UPDATE reviews SET status = 'BLOCKED', error = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (str(exc), utc_now(), review_id),
                )
                connection.execute(
                    """
                    UPDATE codex_sessions SET status = 'BLOCKED', updated_at = ?
                    WHERE id = ?
                    """,
                    (utc_now(), reviewer_session_id),
                )
                self.ledger.append(
                    entity_type="review",
                    entity_id=review_id,
                    event_type="review.blocked",
                    payload={"experiment_id": experiment_id, "error": str(exc)},
                    connection=connection,
                )
            self._transition(
                experiment_id,
                ExperimentStatus.REVIEW_BLOCKED,
                reason="reviewer invocation failed or output was invalid",
            )
            return
        (bundle_dir / "review-output.json").write_text(
            output.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        output_sha256 = sha256_file(bundle_dir / "review-output.json")
        structural_error = self._review_structure_error(
            output,
            bundle_sha=bundle_sha,
            allowed_event_refs=event_refs,
            allowed_sha_refs=sha_refs,
        )
        with self.db.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE reviews SET status = ?, output_json = ?, thread_id = ?,
                    finished_at = ?, error = ? WHERE id = ?
                """,
                (
                    "BLOCKED" if structural_error else "COMPLETED",
                    output.model_dump_json(),
                    thread_id,
                    utc_now(),
                    structural_error,
                    review_id,
                ),
            )
            connection.execute(
                """
                UPDATE codex_sessions SET thread_id = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    thread_id,
                    "BLOCKED" if structural_error else "COMPLETED",
                    utc_now(),
                    reviewer_session_id,
                ),
            )
            self.ledger.append(
                entity_type="review",
                entity_id=review_id,
                event_type="review.output_received",
                payload={
                    "experiment_id": experiment_id,
                    "bundle_sha256": bundle_sha,
                    "review_output_sha256": output_sha256,
                    "verdict": output.verdict,
                    "structural_error": structural_error,
                },
                actor="reviewer",
                connection=connection,
            )
        if structural_error:
            self._transition(
                experiment_id,
                ExperimentStatus.REVIEW_BLOCKED,
                reason="reviewer output failed closed",
                payload={"error": structural_error},
            )
        elif output.verdict == "needs_human":
            self._transition(
                experiment_id,
                ExperimentStatus.NEEDS_HUMAN,
                reason="reviewer requested human judgment",
            )
        elif output.verdict == "ineligible" or any(
            check.status != "pass" for check in output.checks
        ):
            self._transition(
                experiment_id,
                ExperimentStatus.REJECTED,
                reason="reviewer found completed result ineligible",
                payload={"summary": output.summary},
            )
        else:
            self._accept(experiment_id, review_id, output.change_summary)

    @staticmethod
    def _review_structure_error(
        output: ReviewOutput,
        *,
        bundle_sha: str,
        allowed_event_refs: set[str],
        allowed_sha_refs: set[str],
    ) -> str | None:
        if output.target_bundle_sha256 != bundle_sha:
            return "target bundle hash mismatch"
        names = [check.name for check in output.checks]
        if len(names) != len(REQUIRED_REVIEW_CHECKS) or set(names) != REQUIRED_REVIEW_CHECKS:
            return "required checks are missing, duplicated, or unexpected"
        allowed = allowed_event_refs | allowed_sha_refs
        for check in output.checks:
            if not check.evidence_refs:
                return f"check {check.name} has no evidence reference"
            if any(reference not in allowed for reference in check.evidence_refs):
                return f"check {check.name} has an invalid evidence reference"
        if any(reference not in allowed for reference in output.change_summary.evidence_refs):
            return "change summary has an invalid evidence reference"
        return None

    def _accept(
        self, experiment_id: str, review_id: str, change_summary: ChangeSummary
    ) -> None:
        experiment = self._experiment_row(experiment_id)
        policy = self._policy_for_experiment(experiment)
        run = self._run_for_experiment(experiment_id)
        computed = self._metric_rows(experiment_id, source="computed")
        primary = next((row for row in computed if row["name"] == policy.primary_metric), None)
        if primary is None:
            self._transition(
                experiment_id,
                ExperimentStatus.VERIFICATION_FAILED,
                reason="computed primary metric disappeared before acceptance",
            )
            return
        now = utc_now()
        with self.db.transaction(immediate=True) as connection:
            current = ExperimentStatus(
                connection.execute(
                    "SELECT status FROM experiments WHERE id = ?", (experiment_id,)
                ).fetchone()["status"]
            )
            if current != ExperimentStatus.REVIEW_PENDING:
                raise InvalidState(f"cannot accept from {current}")
            for metric in computed:
                connection.execute(
                    """
                    INSERT INTO metrics(
                        experiment_id, run_id, name, value, source,
                        comparison_key, created_at
                    ) VALUES (?, ?, ?, ?, 'verified', ?, ?)
                    """,
                    (
                        experiment_id,
                        run["id"],
                        metric["name"],
                        metric["value"],
                        experiment["comparison_key"],
                        now,
                    ),
                )
            narrative = change_summary.model_dump(mode="json")
            narrative_sha256 = canonical_sha256(narrative)
            self.ledger.append(
                entity_type="experiment",
                entity_id=experiment_id,
                event_type="experiment.change_summary_recorded",
                payload={
                    "schema_version": 1,
                    "source": "independent_reviewer",
                    "review_id": review_id,
                    "change_summary_sha256": narrative_sha256,
                    "change_summary": narrative,
                },
                actor="reviewer",
                connection=connection,
            )
            connection.execute(
                """
                INSERT INTO leaderboard_entries(
                    experiment_id, review_id, comparison_key, metric_name, score,
                    direction, verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    review_id,
                    experiment["comparison_key"],
                    policy.primary_metric,
                    primary["value"],
                    policy.direction,
                    now,
                ),
            )
            connection.execute(
                "UPDATE experiments SET status = ?, updated_at = ? WHERE id = ?",
                (ExperimentStatus.ACCEPTED, now, experiment_id),
            )
            self.ledger.append(
                entity_type="experiment",
                entity_id=experiment_id,
                event_type="experiment.accepted",
                payload={
                    "from": ExperimentStatus.REVIEW_PENDING,
                    "to": ExperimentStatus.ACCEPTED,
                    "review_id": review_id,
                    "comparison_key": experiment["comparison_key"],
                    "metric": policy.primary_metric,
                    "score": primary["value"],
                    "change_summary_sha256": narrative_sha256,
                },
                actor="controller",
                connection=connection,
            )

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self.db.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not row:
                raise NotFound(run_id)
            experiment = connection.execute(
                "SELECT status FROM experiments WHERE id = ?", (row["experiment_id"],)
            ).fetchone()
            status = ExperimentStatus(experiment["status"])
            if status not in (ExperimentStatus.QUEUED, ExperimentStatus.RUNNING):
                raise InvalidState(f"cannot cancel experiment in {status}")
            connection.execute("UPDATE runs SET cancel_requested = 1 WHERE id = ?", (run_id,))
            self.ledger.append(
                entity_type="run",
                entity_id=run_id,
                event_type="run.cancel_requested",
                payload={"experiment_id": row["experiment_id"]},
                actor="user",
                connection=connection,
            )
        if status == ExperimentStatus.QUEUED:
            self._transition(
                row["experiment_id"], ExperimentStatus.CANCELLED, reason="cancelled before start"
            )
        elif row["pid"] and same_process(row["pid"], row["process_start_ticks"]):
            self.runner.terminate_process_group(row["pid"])
        return {"run_id": run_id, "cancel_requested": True}

    def retry_review(self, review_id: str) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
        if not row:
            raise NotFound(review_id)
        experiment_id = row["experiment_id"]
        status = ExperimentStatus(self._experiment_row(experiment_id)["status"])
        if status not in (ExperimentStatus.REVIEW_BLOCKED, ExperimentStatus.NEEDS_HUMAN):
            raise InvalidState(f"review cannot be retried from {status}")
        self._transition(
            experiment_id, ExperimentStatus.REVIEW_PENDING, reason="review retry requested"
        )
        self._review(experiment_id)
        return self.get_experiment(experiment_id)

    def add_note(self, experiment_id: str, note: str) -> None:
        if not note.strip():
            raise ServiceError("note must not be empty")
        with self.db.transaction(immediate=True) as connection:
            if not connection.execute(
                "SELECT 1 FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone():
                raise NotFound(experiment_id)
            connection.execute(
                "UPDATE experiments SET note = ?, updated_at = ? WHERE id = ?",
                (note, utc_now(), experiment_id),
            )
            self.ledger.append(
                entity_type="experiment",
                entity_id=experiment_id,
                event_type="experiment.note_added",
                payload={"note": note},
                actor="user",
                connection=connection,
            )

    def record_historical_change_summary(
        self, experiment_id: str, change_summary: ChangeSummary
    ) -> dict[str, Any]:
        experiment = self._experiment_row(experiment_id)
        if ExperimentStatus(experiment["status"]) != ExperimentStatus.ACCEPTED:
            raise InvalidState("historical summaries may only be attached to accepted experiments")
        if experiment_id in self._change_summary_records():
            raise InvalidState("experiment already has a canonical change summary")
        related_events = [
            event
            for event in self.ledger.list(after=0, limit=1_000_000)
            if event["entity_id"] == experiment_id
            or event["payload"].get("experiment_id") == experiment_id
        ]
        allowed_event_refs = {f"event:{event['seq']}" for event in related_events}
        if any(reference not in allowed_event_refs for reference in change_summary.evidence_refs):
            raise ServiceError("historical change summary has unrelated evidence references")
        narrative = change_summary.model_dump(mode="json")
        payload = {
            "schema_version": 1,
            "source": "historical_offline_backfill",
            "review_id": None,
            "change_summary_sha256": canonical_sha256(narrative),
            "change_summary": narrative,
        }
        event = self.ledger.append(
            entity_type="experiment",
            entity_id=experiment_id,
            event_type="experiment.change_summary_recorded",
            payload=payload,
            actor="controller",
        )
        return {"experiment_id": experiment_id, "event_seq": event["seq"], **payload}

    def _change_summary_records(self) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        for event in self.ledger.list(after=0, limit=1_000_000):
            if event["event_type"] != "experiment.change_summary_recorded":
                continue
            payload = event["payload"]
            try:
                narrative = ChangeSummary.model_validate(payload["change_summary"])
            except (KeyError, ValidationError):
                continue
            records[event["entity_id"]] = {
                **narrative.model_dump(mode="json"),
                "source": payload.get("source"),
                "review_id": payload.get("review_id"),
                "recorded_at": event["created_at"],
                "event_seq": event["seq"],
            }
        return records

    def withdraw(self, experiment_id: str, reason: str) -> None:
        if not reason.strip():
            raise ServiceError("withdrawal reason must not be empty")
        with self.db.transaction(immediate=True) as connection:
            if not connection.execute(
                "SELECT 1 FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone():
                raise NotFound(experiment_id)
            connection.execute(
                "UPDATE experiments SET withdrawn = 1, updated_at = ? WHERE id = ?",
                (utc_now(), experiment_id),
            )
            connection.execute(
                "UPDATE leaderboard_entries SET withdrawn = 1 WHERE experiment_id = ?",
                (experiment_id,),
            )
            self.ledger.append(
                entity_type="experiment",
                entity_id=experiment_id,
                event_type="experiment.withdrawn",
                payload={"reason": reason},
                actor="user",
                connection=connection,
            )

    def recover(self) -> list[dict[str, str]]:
        outcomes: list[dict[str, str]] = []
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT e.id AS experiment_id, e.status AS experiment_status, r.*
                FROM experiments e JOIN runs r ON r.experiment_id = e.id
                WHERE e.status IN (?, ?, ?)
                ORDER BY e.created_at
                """,
                (
                    ExperimentStatus.RUNNING,
                    ExperimentStatus.GRADING,
                    ExperimentStatus.REVIEW_PENDING,
                ),
            ).fetchall()
        for raw in rows:
            row = dict(raw)
            status = ExperimentStatus(row["experiment_status"])
            if status == ExperimentStatus.RUNNING:
                receipt = Path(row["run_dir"]) / "exit-receipt.json"
                if receipt.is_file() and self._recover_receipt(row, receipt):
                    outcomes.append({"experiment_id": row["experiment_id"], "outcome": "receipt"})
                elif row["pid"] and same_process(row["pid"], row["process_start_ticks"]):
                    self.ledger.append(
                        entity_type="run",
                        entity_id=row["id"],
                        event_type="run.recovery_identity_confirmed",
                        payload={
                            "pid": row["pid"],
                            "process_start_ticks": row["process_start_ticks"],
                            "ticket_hash": row["ticket_hash"],
                        },
                    )
                    self._start_recovery_monitor(row)
                    outcomes.append({"experiment_id": row["experiment_id"], "outcome": "running"})
                else:
                    self._transition(
                        row["experiment_id"],
                        ExperimentStatus.ORPHANED,
                        reason="controller cannot confirm prior process identity or exit receipt",
                    )
                    outcomes.append({"experiment_id": row["experiment_id"], "outcome": "orphaned"})
            elif status == ExperimentStatus.GRADING:
                self._grade(row["experiment_id"])
                outcomes.append(
                    {"experiment_id": row["experiment_id"], "outcome": "grading_resumed"}
                )
            else:
                self._review(row["experiment_id"])
                outcomes.append(
                    {"experiment_id": row["experiment_id"], "outcome": "review_resumed"}
                )
        return outcomes

    def _start_recovery_monitor(self, run: dict[str, Any]) -> None:
        run_id = run["id"]
        if run_id in self._recovery_monitors:
            return
        self._recovery_monitors.add(run_id)

        def monitor() -> None:
            try:
                while not self._stop.wait(self.settings.heartbeat_seconds):
                    if same_process(run["pid"], run["process_start_ticks"]):
                        continue
                    receipt = Path(run["run_dir"]) / "exit-receipt.json"
                    current = ExperimentStatus(self._experiment_row(run["experiment_id"])["status"])
                    if current != ExperimentStatus.RUNNING:
                        return
                    if receipt.is_file() and self._recover_receipt(run, receipt):
                        return
                    self._transition(
                        run["experiment_id"],
                        ExperimentStatus.ORPHANED,
                        reason="recovered process disappeared without a valid exit receipt",
                    )
                    return
            finally:
                self._recovery_monitors.discard(run_id)

        threading.Thread(
            target=monitor,
            name=f"verilab-recover-{run_id}",
            daemon=True,
        ).start()

    def _recover_receipt(self, run: dict[str, Any], receipt: Path) -> bool:
        try:
            value = json.loads(receipt.read_text(encoding="utf-8"))
            experiment = self._experiment_row(run["experiment_id"])
            if value.get("schema_version") != 1:
                return False
            if value.get("commit") != experiment["git_commit"]:
                return False
            if value.get("command") != json.loads(run["command_json"]):
                return False
            if run.get("command_fingerprint") and (
                value.get("command_fingerprint") != run["command_fingerprint"]
            ):
                return False
            exit_code = int(value["exit_code"])
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False
        digest = sha256_file(receipt)
        with self.db.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE runs SET status = 'FINISHED', finished_at = ?, exit_code = ?,
                    exit_receipt_sha256 = ? WHERE id = ?
                """,
                (value.get("finished_at") or utc_now(), exit_code, digest, run["id"]),
            )
            self.ledger.append(
                entity_type="run",
                entity_id=run["id"],
                event_type="run.exit_receipt_recovered",
                payload={
                    "experiment_id": run["experiment_id"],
                    "ticket_hash": run["ticket_hash"],
                    "receipt_sha256": digest,
                    "exit_code": exit_code,
                },
                connection=connection,
            )
        if run["cancel_requested"]:
            self._transition(
                run["experiment_id"],
                ExperimentStatus.CANCELLED,
                reason="recovered cancelled run receipt",
            )
        elif exit_code != 0:
            self._transition(
                run["experiment_id"],
                ExperimentStatus.FAILED,
                reason=f"recovered formal command exited {exit_code}",
            )
        else:
            self._transition(
                run["experiment_id"],
                ExperimentStatus.GRADING,
                reason="valid exit receipt recovered after controller restart",
            )
            self._grade(run["experiment_id"])
        return True

    def refresh_evidence_health(self) -> list[dict[str, str]]:
        changes: list[dict[str, str]] = []
        with self.db.connect() as connection:
            artifacts = [dict(row) for row in connection.execute("SELECT * FROM artifacts")]
        for artifact in artifacts:
            _valid, health = self.artifact_store.verify_record(artifact, pre_review=False)
            if artifact["health"] != "healthy" and health == "healthy":
                # Restoring the exact bytes helps future recomputation but does not erase
                # the historical evidence-loss event or automatically restore trust health.
                continue
            if health == artifact["health"]:
                continue
            with self.db.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE artifacts SET health = ? WHERE id = ?", (health, artifact["id"])
                )
                if artifact["required"] and health != "healthy":
                    connection.execute(
                        "UPDATE experiments SET evidence_health = 'degraded' WHERE id = ?",
                        (artifact["experiment_id"],),
                    )
                    connection.execute(
                        """
                        UPDATE leaderboard_entries SET evidence_health = 'degraded'
                        WHERE experiment_id = ?
                        """,
                        (artifact["experiment_id"],),
                    )
                self.ledger.append(
                    entity_type="artifact",
                    entity_id=artifact["id"],
                    event_type="artifact.health_changed",
                    payload={
                        "experiment_id": artifact["experiment_id"],
                        "from": artifact["health"],
                        "to": health,
                    },
                    connection=connection,
                )
            changes.append({"artifact_id": artifact["id"], "health": health})
        return changes

    def audit_verify(self, *, refresh_artifacts: bool = True) -> dict[str, Any]:
        changes = self.refresh_evidence_health() if refresh_artifacts else []
        chain = self.ledger.verify().to_dict()
        projection_errors = self._verify_projections()
        with self.db.connect() as connection:
            health = {
                row["health"]: row["count"]
                for row in connection.execute(
                    "SELECT health, COUNT(*) AS count FROM artifacts GROUP BY health"
                )
            }
        return {
            "ok": chain["ok"] and not projection_errors,
            "chain": chain,
            "projections": {"ok": not projection_errors, "errors": projection_errors},
            "artifact_health": health,
            "health_changes": changes,
        }

    def _verify_projections(self) -> list[str]:
        statuses: dict[str, str] = {}
        accepted: dict[str, dict[str, Any]] = {}
        withdrawn: set[str] = set()
        sealed_artifacts: dict[str, dict[str, Any]] = {}
        artifact_health: dict[str, str] = {}
        computed_metrics: dict[tuple[str, str], float] = {}
        narrative_hashes: dict[str, str] = {}
        event_errors: list[str] = []
        try:
            events = self.ledger.list(after=0, limit=1_000_000)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return [f"canonical events cannot rebuild projections: {exc}"]
        for event in events:
            payload = event["payload"]
            event_type = event["event_type"]
            if event_type == "experiment.submitted":
                statuses[event["entity_id"]] = ExperimentStatus.QUEUED
            elif event_type == "experiment.status_changed":
                statuses[event["entity_id"]] = str(payload.get("to"))
            elif event_type == "experiment.change_summary_recorded":
                try:
                    narrative = ChangeSummary.model_validate(payload["change_summary"])
                    actual_hash = canonical_sha256(narrative.model_dump(mode="json"))
                    if actual_hash != payload.get("change_summary_sha256"):
                        event_errors.append(
                            f"change summary hash mismatch for {event['entity_id']}"
                        )
                    narrative_hashes[event["entity_id"]] = actual_hash
                except (KeyError, ValidationError) as exc:
                    event_errors.append(
                        f"invalid change summary event for {event['entity_id']}: {exc}"
                    )
            elif event_type == "experiment.accepted":
                experiment_id = event["entity_id"]
                statuses[experiment_id] = ExperimentStatus.ACCEPTED
                accepted[experiment_id] = payload
                narrative_hash = payload.get("change_summary_sha256")
                if narrative_hash and narrative_hashes.get(experiment_id) != narrative_hash:
                    event_errors.append(
                        f"experiment {experiment_id} accepted without its recorded change summary"
                    )
            elif event_type == "experiment.withdrawn":
                withdrawn.add(event["entity_id"])
            elif event_type == "artifact.sealed":
                sealed_artifacts[event["entity_id"]] = payload
                artifact_health[event["entity_id"]] = "healthy"
            elif event_type == "artifact.health_changed":
                artifact_health[event["entity_id"]] = str(payload.get("to"))
            elif event_type == "grader.completed":
                for name, value in payload.get("metrics", {}).items():
                    computed_metrics[(event["entity_id"], name)] = float(value)

        errors: list[str] = list(event_errors)
        with self.db.connect() as connection:
            experiment_rows = {
                row["id"]: dict(row) for row in connection.execute("SELECT * FROM experiments")
            }
            leaderboard_rows = {
                row["experiment_id"]: dict(row)
                for row in connection.execute("SELECT * FROM leaderboard_entries")
            }
            artifact_rows = {
                row["id"]: dict(row) for row in connection.execute("SELECT * FROM artifacts")
            }
            metric_rows = {
                (row["experiment_id"], row["name"], row["source"]): float(row["value"])
                for row in connection.execute("SELECT * FROM metrics")
            }
        degraded_experiments = {
            payload["experiment_id"]
            for artifact_id, payload in sealed_artifacts.items()
            if payload.get("required") and artifact_health.get(artifact_id, "healthy") != "healthy"
        }
        for experiment_id, expected_status in statuses.items():
            row = experiment_rows.get(experiment_id)
            if row is None:
                errors.append(f"experiment projection missing {experiment_id}")
            elif row["status"] != expected_status:
                errors.append(
                    f"experiment {experiment_id} status projection mismatch: "
                    f"{row['status']} != {expected_status}"
                )
            if row is not None:
                try:
                    policy = self.settings.load_policy(row["policy_hash"])
                    if policy.comparison_key != row["comparison_key"]:
                        errors.append(
                            f"experiment {experiment_id} comparison-key projection mismatch"
                        )
                except RuntimeError as exc:
                    errors.append(f"experiment {experiment_id} policy snapshot invalid: {exc}")
                expected_health = "degraded" if experiment_id in degraded_experiments else "healthy"
                if row["evidence_health"] != expected_health:
                    errors.append(f"experiment {experiment_id} evidence-health projection mismatch")
        for experiment_id, payload in accepted.items():
            row = leaderboard_rows.get(experiment_id)
            if row is None:
                errors.append(f"leaderboard projection missing {experiment_id}")
                continue
            expected = {
                "review_id": payload.get("review_id"),
                "comparison_key": payload.get("comparison_key"),
                "metric_name": payload.get("metric"),
                "score": float(payload.get("score")),
                "withdrawn": int(experiment_id in withdrawn),
            }
            for key, value in expected.items():
                if row[key] != value:
                    errors.append(f"leaderboard {experiment_id} {key} projection mismatch")
            expected_health = "degraded" if experiment_id in degraded_experiments else "healthy"
            if row["evidence_health"] != expected_health:
                errors.append(f"leaderboard {experiment_id} evidence-health projection mismatch")
            verified = metric_rows.get((experiment_id, str(payload.get("metric")), "verified"))
            if verified != float(payload.get("score")):
                errors.append(f"verified metric projection mismatch for {experiment_id}")
        for experiment_id in set(leaderboard_rows) - set(accepted):
            errors.append(f"leaderboard has unauthenticated extra row {experiment_id}")
        for artifact_id, payload in sealed_artifacts.items():
            row = artifact_rows.get(artifact_id)
            if row is None:
                errors.append(f"artifact projection missing {artifact_id}")
                continue
            for key in ("experiment_id", "run_id", "role", "relative_path", "sha256", "size_bytes"):
                if row[key] != payload.get(key):
                    errors.append(f"artifact {artifact_id} {key} projection mismatch")
            if row["health"] != artifact_health.get(artifact_id, "healthy"):
                errors.append(f"artifact {artifact_id} health projection mismatch")
        for artifact_id in set(artifact_rows) - set(sealed_artifacts):
            errors.append(f"artifact projection has unauthenticated extra row {artifact_id}")
        for (experiment_id, name), value in computed_metrics.items():
            if metric_rows.get((experiment_id, name, "computed")) != value:
                errors.append(f"computed metric projection mismatch for {experiment_id}:{name}")
        return errors

    def get_experiment(self, experiment_id: str) -> dict[str, Any]:
        experiment = self._experiment_row(experiment_id)
        experiment["spec"] = self._public_spec(
            experiment.pop("spec_json"), self._policy_for_experiment(experiment)
        )
        experiment["run"] = self._run_for_experiment(experiment_id)
        experiment["metrics"] = self._metric_rows(experiment_id)
        experiment["artifacts"] = self._artifact_rows(experiment_id)
        with self.db.connect() as connection:
            experiment["reviews"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM reviews WHERE experiment_id = ? ORDER BY attempt",
                    (experiment_id,),
                )
            ]
        for review in experiment["reviews"]:
            review["output"] = json.loads(review["output_json"]) if review["output_json"] else None
        experiment["parent_diff"] = {}
        if experiment["parent_experiment_id"]:
            parent = self._experiment_row(experiment["parent_experiment_id"])
            parent_spec = self._public_spec(
                parent["spec_json"], self._policy_for_experiment(parent)
            )
            current_spec = experiment["spec"]
            experiment["parent_diff"] = {
                key: {"parent": parent_spec.get(key), "current": current_spec.get(key)}
                for key in sorted(set(parent_spec) | set(current_spec))
                if parent_spec.get(key) != current_spec.get(key)
            }
        experiment["change_summary"] = self._change_summary_records().get(experiment_id)
        stage_by_status = {
            ExperimentStatus.DRAFT: 1,
            ExperimentStatus.QUEUED: 2,
            ExperimentStatus.RUNNING: 3,
            ExperimentStatus.GRADING: 4,
            ExperimentStatus.REVIEW_PENDING: 5,
            ExperimentStatus.REVIEW_BLOCKED: 5,
            ExperimentStatus.NEEDS_HUMAN: 5,
            ExperimentStatus.ACCEPTED: 6,
            ExperimentStatus.REJECTED: 5,
            ExperimentStatus.VERIFICATION_FAILED: 4,
            ExperimentStatus.FAILED: 3,
            ExperimentStatus.CANCELLED: 3,
            ExperimentStatus.ORPHANED: 3,
        }
        experiment["pipeline_stage"] = stage_by_status[ExperimentStatus(experiment["status"])]
        experiment["events"] = [
            event
            for event in self.ledger.list(after=0, limit=100000)
            if event["entity_id"] in {experiment_id, experiment["run"]["id"]}
            or event["payload"].get("experiment_id") == experiment_id
        ]
        return experiment

    @staticmethod
    def _public_spec(spec_json: str, policy: ProjectPolicy) -> dict[str, Any]:
        value = json.loads(spec_json)
        value["env"] = redacted_environment(value.get("env", {}), policy.secret_names)
        value["secret_refs"] = ["[redacted]"] * len(value.get("secret_refs", []))
        return value

    def list_experiments(self, *, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM experiments"
        params: tuple[object, ...] = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY created_at DESC"
        with self.db.connect() as connection:
            rows = [dict(row) for row in connection.execute(query, params)]
        for row in rows:
            row.pop("spec_json", None)
        return rows

    def experiment_lineage(self, comparison_key: str | None = None) -> dict[str, Any]:
        key = comparison_key or self.policy.comparison_key
        with self.db.connect() as connection:
            experiments = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM experiments
                    WHERE comparison_key = ?
                    ORDER BY created_at, id
                    """,
                    (key,),
                )
            ]
            metric_rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT experiment_id, name, value, source
                    FROM metrics
                    WHERE comparison_key = ?
                    """,
                    (key,),
                )
            ]
        if not experiments:
            return {"comparison_key": key, "primary_metric": None, "nodes": []}

        policy = self._policy_for_experiment(experiments[0])
        source_priority = {"reported": 0, "computed": 1, "verified": 2}
        scores: dict[str, dict[str, Any]] = {}
        for metric in metric_rows:
            if metric["name"] != policy.primary_metric:
                continue
            current = scores.get(metric["experiment_id"])
            if current is None or source_priority.get(metric["source"], -1) > source_priority.get(
                current["source"], -1
            ):
                scores[metric["experiment_id"]] = metric

        specs = {
            row["id"]: self._public_spec(row["spec_json"], self._policy_for_experiment(row))
            for row in experiments
        }
        by_id = {row["id"]: row for row in experiments}
        change_summaries = self._change_summary_records()
        git_cache: dict[tuple[str, str], tuple[int, list[dict[str, str]]]] = {}
        excluded_fields = {
            "git_commit",
            "parent_experiment_id",
            "schema_version",
            "secret_refs",
            "title",
        }
        nodes: list[dict[str, Any]] = []
        for experiment in experiments:
            experiment_id = experiment["id"]
            parent_id = experiment["parent_experiment_id"]
            score = scores.get(experiment_id)
            parent_score = scores.get(parent_id) if parent_id else None
            spec_changes: list[dict[str, Any]] = []
            git_change_count = 0
            git_changes: list[dict[str, str]] = []
            parent_title = None
            if parent_id and parent_id in by_id:
                parent = by_id[parent_id]
                parent_title = parent["title"]
                parent_spec = specs[parent_id]
                current_spec = specs[experiment_id]
                spec_changes = [
                    {
                        "field": field,
                        "parent": parent_spec.get(field),
                        "current": current_spec.get(field),
                    }
                    for field in sorted(set(parent_spec) | set(current_spec))
                    if field not in excluded_fields
                    and parent_spec.get(field) != current_spec.get(field)
                ]
                commit_pair = (parent["git_commit"], experiment["git_commit"])
                if commit_pair not in git_cache:
                    git_cache[commit_pair] = self._git_change_summary(*commit_pair)
                git_change_count, git_changes = git_cache[commit_pair]
            nodes.append(
                {
                    "id": experiment_id,
                    "title": experiment["title"],
                    "parent_experiment_id": parent_id,
                    "parent_title": parent_title,
                    "status": experiment["status"],
                    "withdrawn": bool(experiment["withdrawn"]),
                    "evidence_health": experiment["evidence_health"],
                    "note": experiment["note"],
                    "change_summary": change_summaries.get(experiment_id),
                    "git_commit": experiment["git_commit"],
                    "created_at": experiment["created_at"],
                    "score": score["value"] if score else None,
                    "score_source": score["source"] if score else None,
                    "parent_delta": (
                        score["value"] - parent_score["value"]
                        if score is not None and parent_score is not None
                        else None
                    ),
                    "spec_changes": spec_changes,
                    "git_change_count": git_change_count,
                    "git_changes": git_changes,
                }
            )
        return {
            "comparison_key": key,
            "primary_metric": policy.primary_metric,
            "nodes": nodes,
        }

    def _git_change_summary(
        self, parent_commit: str, current_commit: str
    ) -> tuple[int, list[dict[str, str]]]:
        if parent_commit == current_commit:
            return 0, []
        result = repository_git(
            self.settings.project_root,
            "diff",
            "--name-status",
            "-z",
            parent_commit,
            current_commit,
            "--",
            check=False,
        )
        if result.returncode != 0:
            return 0, []
        tokens = result.stdout.split("\0")
        changes: list[dict[str, str]] = []
        index = 0
        while index < len(tokens) and tokens[index]:
            status = tokens[index]
            index += 1
            if index >= len(tokens) or not tokens[index]:
                break
            if status.startswith(("R", "C")):
                if index + 1 >= len(tokens) or not tokens[index + 1]:
                    break
                changes.append(
                    {
                        "status": status,
                        "old_path": tokens[index],
                        "path": tokens[index + 1],
                    }
                )
                index += 2
            else:
                changes.append({"status": status, "path": tokens[index]})
                index += 1
        return len(changes), changes[:60]

    def leaderboard(self, comparison_key: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT l.*, e.title, e.git_commit, e.parent_experiment_id,
                   e.protocol_id, r.started_at, r.finished_at,
                   v.status AS reviewer_status
            FROM leaderboard_entries l
            JOIN experiments e ON e.id = l.experiment_id
            JOIN runs r ON r.experiment_id = e.id
            JOIN reviews v ON v.id = l.review_id
            WHERE l.withdrawn = 0
        """
        params: tuple[object, ...] = ()
        if comparison_key:
            query += " AND l.comparison_key = ?"
            params = (comparison_key,)
        query += """
            ORDER BY l.comparison_key,
                CASE WHEN l.direction = 'maximize' THEN -l.score ELSE l.score END,
                l.verified_at
        """
        with self.db.connect() as connection:
            rows = [dict(row) for row in connection.execute(query, params)]
            scores = {
                row["experiment_id"]: row["score"]
                for row in connection.execute(
                    "SELECT experiment_id, score FROM leaderboard_entries"
                )
            }
        ranks: dict[str, int] = {}
        change_summaries = self._change_summary_records()
        for row in rows:
            key = row["comparison_key"]
            ranks[key] = ranks.get(key, 0) + 1
            row["rank"] = ranks[key]
            parent_score = scores.get(row["parent_experiment_id"])
            row["parent_delta"] = row["score"] - parent_score if parent_score is not None else None
            row["change_summary"] = change_summaries.get(row["experiment_id"])
            row["duration_seconds"] = None
            if row["started_at"] and row["finished_at"]:
                row["duration_seconds"] = (
                    datetime.fromisoformat(row["finished_at"])
                    - datetime.fromisoformat(row["started_at"])
                ).total_seconds()
        return rows

    def latest_review_bundle(self, experiment_id: str) -> Path:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT id FROM reviews WHERE experiment_id = ? ORDER BY attempt DESC LIMIT 1",
                (experiment_id,),
            ).fetchone()
        if not row:
            raise NotFound("experiment has no review bundle")
        return self.settings.state_dir / "reviewer-bundles" / row["id"]

    def chat(self, message: str) -> dict[str, Any]:
        if not message.strip():
            raise ServiceError("chat message must not be empty")
        now = utc_now()
        with self.db.transaction(immediate=True) as connection:
            session = connection.execute(
                """
                SELECT * FROM codex_sessions
                WHERE role = 'executor' AND status = 'ACTIVE'
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
            if session is None:
                session_id = _id("session")
                connection.execute(
                    """
                    INSERT INTO codex_sessions(id, role, status, created_at, updated_at)
                    VALUES (?, 'executor', 'ACTIVE', ?, ?)
                    """,
                    (session_id, now, now),
                )
                thread_id = None
            else:
                session_id = session["id"]
                thread_id = session["thread_id"]
            connection.execute(
                """
                INSERT INTO messages(id, session_id, role, content, created_at)
                VALUES (?, ?, 'user', ?, ?)
                """,
                (_id("msg"), session_id, message, now),
            )
            self.ledger.append(
                entity_type="codex_session",
                entity_id=session_id,
                event_type="chat.user_message",
                payload={"message_id": "stored", "content_sha256": canonical_sha256(message)},
                actor="user",
                connection=connection,
            )
        api_url = f"http://{self.settings.host}:{self.settings.port}"
        prompt = f"""You are the VeriLab Executor for repository {self.settings.project_root}.
Help the user implement and run experiments. Formal results must be submitted as an immutable
ExperimentSpec v1 with `verilab submit <spec.json>`. You may use only `verilab submit`,
`verilab status`, `verilab follow`, and `verilab cancel` for Controller operations. Do not write
the Controller database, trusted policy, grader output, Reviewer output, or leaderboard. Commands
you run directly are debugging and are untracked. Commit a clean Git state before formal submit.

User message:
{message}
"""
        environment = sanitized_process_environment()
        environment.update(
            {
                "VERILAB_API_URL": api_url,
                "VERILAB_CAPABILITY_FILE": str(self.settings.state_dir / "capability.token"),
                "VERILAB_PROJECT_ROOT": str(self.settings.project_root),
            }
        )
        result = self.codex_driver.executor(
            prompt=prompt,
            cwd=self.settings.project_root,
            output_dir=self.settings.state_dir / "codex",
            session_id=thread_id,
            environment=environment,
        )
        finished = utc_now()
        with self.db.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE codex_sessions SET thread_id = ?, updated_at = ? WHERE id = ?",
                (result.thread_id, finished, session_id),
            )
            connection.execute(
                """
                INSERT INTO messages(id, session_id, role, content, created_at)
                VALUES (?, ?, 'assistant', ?, ?)
                """,
                (_id("msg"), session_id, result.final_text, finished),
            )
            self.ledger.append(
                entity_type="codex_session",
                entity_id=session_id,
                event_type="chat.assistant_message",
                payload={
                    "thread_id": result.thread_id,
                    "content_sha256": canonical_sha256(result.final_text),
                    "event_count": len(result.events),
                },
                actor="executor",
                connection=connection,
            )
        return {
            "session_id": session_id,
            "thread_id": result.thread_id,
            "message": result.final_text,
        }

    def _experiment_row(self, experiment_id: str) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
        if not row:
            raise NotFound(experiment_id)
        return dict(row)

    def _run_row(self, run_id: str) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise NotFound(run_id)
        return dict(row)

    def _run_for_experiment(self, experiment_id: str) -> dict[str, Any]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE experiment_id = ?", (experiment_id,)
            ).fetchone()
        if not row:
            raise NotFound(f"run for {experiment_id}")
        return dict(row)

    def _artifact_rows(self, experiment_id: str) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM artifacts WHERE experiment_id = ? ORDER BY role, relative_path",
                    (experiment_id,),
                )
            ]

    def _metric_rows(self, experiment_id: str, source: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM metrics WHERE experiment_id = ?"
        params: tuple[object, ...] = (experiment_id,)
        if source:
            query += " AND source = ?"
            params += (source,)
        query += " ORDER BY name, source"
        with self.db.connect() as connection:
            return [dict(row) for row in connection.execute(query, params)]
