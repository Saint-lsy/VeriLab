from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from verilab.models import ChangeSummary, ExperimentSpec, ReviewOutput
from verilab.security import process_start_ticks, same_process
from verilab.service import InvalidState

from .conftest import FakeReviewer, commit_all, load_policy, make_spec


def test_spec_rejects_shell_strings_and_escaping_paths(project: Path) -> None:
    value = make_spec(project).model_dump(mode="json")
    value["command"] = "python train.py"
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(value)
    value = make_spec(project).model_dump(mode="json")
    value["env"] = {"OPENAI_API_KEY": "must-not-be-inline"}
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(value)
    value = make_spec(project).model_dump(mode="json")
    value["env"] = {"CUDA_VISIBLE_DEVICES": "1"}
    value["resource_claim"]["gpu_ids"] = ["0"]
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(value)
    value = make_spec(project).model_dump(mode="json")
    value["cwd"] = "../outside"
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(value)
    value = make_spec(project).model_dump(mode="json")
    value["expected_artifacts"][0]["glob"] = "/tmp/*.json"
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(value)


def test_duplicate_submit_has_one_run_ticket(project: Path, service_factory) -> None:
    service = service_factory()
    spec = make_spec(project)
    first = service.submit(spec)
    second = service.submit(spec)
    assert first["run_id"] == second["run_id"]
    assert second["deduplicated"] is True
    with service.db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_valid_low_score_is_verified_and_ranked(project: Path, service_factory) -> None:
    service = service_factory()
    submitted = service.submit(make_spec(project, title="Honest low result"))
    assert service.run_next() is True
    result = service.get_experiment(submitted["experiment_id"])
    assert result["status"] == "ACCEPTED"
    assert {(row["source"], row["value"]) for row in result["metrics"]} == {
        ("reported", 0.75),
        ("computed", 0.75),
        ("verified", 0.75),
    }
    board = service.leaderboard()
    assert len(board) == 1
    assert board[0]["score"] == 0.75
    assert board[0]["rank"] == 1
    assert board[0]["change_summary"]["source"] == "independent_reviewer"
    assert result["change_summary"]["headline"]
    event_types = [event["event_type"] for event in result["events"]]
    assert event_types.index("experiment.change_summary_recorded") < event_types.index(
        "experiment.accepted"
    )
    assert service.audit_verify()["ok"] is True


def test_missing_natural_language_summary_fails_closed(project: Path, service_factory) -> None:
    class MissingSummaryReviewer:
        def review(self, **_kwargs):
            return ReviewOutput.model_validate(
                {
                    "target_bundle_sha256": "0" * 64,
                    "verdict": "eligible",
                    "checks": [],
                    "summary": "A review without the required human narrative.",
                }
            )

    service = service_factory(reviewer=MissingSummaryReviewer())
    submitted = service.submit(make_spec(project, title="Missing narrative"))
    service.run_next()
    result = service.get_experiment(submitted["experiment_id"])
    assert result["status"] == "REVIEW_BLOCKED"
    assert result["change_summary"] is None
    assert service.leaderboard() == []


def test_reviewer_summary_cannot_be_replaced_by_historical_backfill(
    project: Path, service_factory
) -> None:
    service = service_factory()
    submitted = service.submit(make_spec(project, title="Historical narrative"))
    service.run_next()
    experiment_id = submitted["experiment_id"]
    related = [
        event
        for event in service.ledger.list(after=0, limit=1000)
        if event["entity_id"] == experiment_id
    ]
    reference = f"event:{related[0]['seq']}"
    summary = ChangeSummary.model_validate(
        {
            "headline": "Historical result explained for human review",
            "summary": "This appended explanation preserves the original experiment evidence.",
            "key_changes": ["The historical result is described without changing its score."],
            "expected_effect": "Make the experiment progression understandable to a human.",
            "observed_effect": "The trusted score and leaderboard row remain unchanged.",
            "evidence_refs": [reference],
        }
    )
    with pytest.raises(InvalidState, match="already has a canonical change summary"):
        service.record_historical_change_summary(experiment_id, summary)
    result = service.get_experiment(experiment_id)
    assert result["change_summary"]["source"] == "independent_reviewer"
    assert service.leaderboard()[0]["score"] == 0.75
    assert service.audit_verify()["ok"] is True


def test_forged_untrusted_score_json_never_controls_leaderboard(
    project: Path, service_factory
) -> None:
    (project / "forge.py").write_text(
        """from pathlib import Path
import json, os
from train import main
main()
Path(os.environ['VERILAB_RUN_DIR'], 'score.json').write_text(json.dumps({'accuracy': 999.0}))
""",
        encoding="utf-8",
    )
    commit_all(project, "add forged score output")
    service = service_factory()
    spec = make_spec(
        project,
        title="Forged score is ignored",
        command=[
            "python3",
            "forge.py",
            "--predictions",
            "0,1,0,0",
            "--reported-score",
            "0.75",
        ],
    )
    submitted = service.submit(spec)
    service.run_next()
    result = service.get_experiment(submitted["experiment_id"])
    assert result["status"] == "ACCEPTED"
    assert service.leaderboard()[0]["score"] == 0.75
    assert all(artifact["relative_path"] != "score.json" for artifact in result["artifacts"])


def test_reported_metric_mismatch_fails_verification(project: Path, service_factory) -> None:
    service = service_factory()
    spec = make_spec(
        project,
        title="Mismatched reported score",
        command=[
            "python3",
            "train.py",
            "--predictions",
            "0,1,0,0",
            "--reported-score",
            "0.99",
        ],
    )
    submitted = service.submit(spec)
    service.run_next()
    assert service.get_experiment(submitted["experiment_id"])["status"] == "VERIFICATION_FAILED"
    assert service.leaderboard() == []


@pytest.mark.parametrize(
    ("reviewer", "expected"),
    [
        (FakeReviewer(raises=TimeoutError("review timed out")), "REVIEW_BLOCKED"),
        (FakeReviewer(missing_check=True), "REVIEW_BLOCKED"),
        (FakeReviewer(bad_bundle=True), "REVIEW_BLOCKED"),
        (FakeReviewer(bad_reference=True), "REVIEW_BLOCKED"),
        (FakeReviewer(verdict="ineligible", check_status="fail"), "REJECTED"),
        (FakeReviewer(verdict="eligible", check_status="unknown"), "REJECTED"),
        (FakeReviewer(verdict="needs_human"), "NEEDS_HUMAN"),
    ],
)
def test_reviewer_fail_closed(project: Path, service_factory, reviewer, expected: str) -> None:
    service = service_factory(reviewer)
    submitted = service.submit(make_spec(project, title=f"Reviewer path {expected}"))
    service.run_next()
    assert service.get_experiment(submitted["experiment_id"])["status"] == expected
    assert service.leaderboard() == []


def test_artifact_modified_before_review_fails_verification(project: Path, service_factory) -> None:
    service = service_factory()
    original_review = service._review

    def drift_then_review(experiment_id: str) -> None:
        artifact = next(
            row for row in service._artifact_rows(experiment_id) if row["role"] == "predictions"
        )
        Path(artifact["absolute_path"]).write_text("{}\n", encoding="utf-8")
        original_review(experiment_id)

    service._review = drift_then_review  # type: ignore[method-assign]
    submitted = service.submit(make_spec(project, title="Drift before review"))
    service.run_next()
    assert service.get_experiment(submitted["experiment_id"])["status"] == "VERIFICATION_FAILED"


def test_comparison_keys_never_mix(project: Path, service_factory) -> None:
    service = service_factory(state_name="shared-state")
    first = service.submit(make_spec(project, title="Policy one"))
    service.run_next()
    key_one = service.get_experiment(first["experiment_id"])["comparison_key"]

    policy_two = load_policy(project, preprocessing="identity-v2")
    service.settings.install_policy(policy_two)
    second = service.submit(make_spec(project, title="Policy two"))
    service.run_next()
    key_two = service.get_experiment(second["experiment_id"])["comparison_key"]
    assert key_one != key_two
    all_rows = service.leaderboard()
    assert len(all_rows) == 2
    assert [row["rank"] for row in all_rows] == [1, 1]
    assert len(service.leaderboard(key_one)) == 1
    assert len(service.leaderboard(key_two)) == 1


def test_queued_experiment_uses_its_pinned_policy_snapshot(project: Path, service_factory) -> None:
    service = service_factory(state_name="policy-snapshot-state")
    submitted = service.submit(make_spec(project, title="Pinned before policy change"))
    original = service.get_experiment(submitted["experiment_id"])
    original_hash = original["policy_hash"]
    original_key = original["comparison_key"]
    replacement = load_policy(project, preprocessing="new-controller-default")
    service.settings.install_policy(replacement)
    assert replacement.policy_hash != original_hash
    service.run_next()
    result = service.get_experiment(submitted["experiment_id"])
    assert result["status"] == "ACCEPTED"
    assert result["policy_hash"] == original_hash
    assert service.leaderboard()[0]["comparison_key"] == original_key
    with service.db.connect() as connection:
        metric_keys = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT comparison_key FROM metrics WHERE experiment_id = ?",
                (submitted["experiment_id"],),
            )
        }
    assert metric_keys == {original_key}


def test_cancel_terminates_process_group_and_preserves_evidence(
    project: Path, service_factory
) -> None:
    (project / "long.py").write_text(
        """import os, subprocess, time
from pathlib import Path
child = subprocess.Popen(['sleep', '60'])
Path(os.environ['VERILAB_RUN_DIR'], 'child.pid').write_text(str(child.pid))
time.sleep(60)
""",
        encoding="utf-8",
    )
    commit_all(project, "add long process group test")
    service = service_factory()
    submitted = service.submit(
        make_spec(project, title="Cancellation", command=["python3", "long.py"])
    )
    worker = threading.Thread(target=service.run_next)
    worker.start()
    deadline = time.time() + 10
    run = service._run_row(submitted["run_id"])
    while not run["pid"] and time.time() < deadline:
        time.sleep(0.02)
        run = service._run_row(submitted["run_id"])
    assert run["pid"] and same_process(run["pid"], run["process_start_ticks"])
    child_path = Path(run["run_dir"]) / "child.pid"
    while not child_path.exists() and time.time() < deadline:
        time.sleep(0.02)
    child_pid = int(child_path.read_text(encoding="utf-8"))
    service.cancel(submitted["run_id"])
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert service.get_experiment(submitted["experiment_id"])["status"] == "CANCELLED"
    assert not same_process(run["pid"], run["process_start_ticks"])
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    assert any(
        event["event_type"] == "run.cancel_requested"
        for event in service.get_experiment(submitted["experiment_id"])["events"]
    )
    roles = {row["role"] for row in service._artifact_rows(submitted["experiment_id"])}
    assert {"execution_log", "exit_receipt"}.issubset(roles)


def test_restart_recovery_checks_pid_start_time_and_ticket(project: Path, service_factory) -> None:
    service = service_factory(state_name="recovery-state")
    submitted = service.submit(make_spec(project, title="Recovery identity"))
    process = subprocess.Popen(["sleep", "30"], start_new_session=True)
    ticks = process_start_ticks(process.pid)
    with service.db.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE experiments SET status = 'RUNNING' WHERE id = ?",
            (submitted["experiment_id"],),
        )
        connection.execute(
            "UPDATE runs SET status = 'RUNNING', pid = ?, process_start_ticks = ? WHERE id = ?",
            (process.pid, ticks, submitted["run_id"]),
        )
    recovered = service.recover()
    assert recovered == [{"experiment_id": submitted["experiment_id"], "outcome": "running"}]
    events = service.get_experiment(submitted["experiment_id"])["events"]
    identity = next(
        event for event in events if event["event_type"] == "run.recovery_identity_confirmed"
    )
    assert (
        identity["payload"]["ticket_hash"] == service._run_row(submitted["run_id"])["ticket_hash"]
    )
    os.killpg(process.pid, signal.SIGTERM)
    process.wait(timeout=5)


def test_event_trigger_blocks_edits_and_chain_detects_tampering(
    project: Path, service_factory
) -> None:
    service = service_factory()
    service.submit(make_spec(project))
    with service.db.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE events SET payload_json = '{}' WHERE seq = 1")
    with service.db.connect() as connection:
        connection.execute("DROP TRIGGER events_no_update")
        connection.execute("UPDATE events SET payload_json = '{}' WHERE seq = 1")
    result = service.audit_verify(refresh_artifacts=False)
    assert result["ok"] is False
    assert "hash mismatch" in " ".join(result["chain"]["errors"])


def test_audit_detects_leaderboard_projection_tampering(project: Path, service_factory) -> None:
    service = service_factory()
    submitted = service.submit(make_spec(project, title="Projection tamper"))
    service.run_next()
    with service.db.connect() as connection:
        connection.execute(
            "UPDATE leaderboard_entries SET score = 12345 WHERE experiment_id = ?",
            (submitted["experiment_id"],),
        )
    result = service.audit_verify()
    assert result["chain"]["ok"] is True
    assert result["projections"]["ok"] is False
    assert "score projection mismatch" in " ".join(result["projections"]["errors"])


def test_blocked_review_can_retry_but_cannot_be_force_verified(
    project: Path, service_factory
) -> None:
    service = service_factory(FakeReviewer(raises=TimeoutError("temporary")))
    submitted = service.submit(make_spec(project, title="Review retry"))
    service.run_next()
    result = service.get_experiment(submitted["experiment_id"])
    assert result["status"] == "REVIEW_BLOCKED"
    service.reviewer = FakeReviewer()
    retried = service.retry_review(result["reviews"][0]["id"])
    assert retried["status"] == "ACCEPTED"
    assert len(retried["reviews"]) == 2
    assert len(service.leaderboard()) == 1


def test_large_artifact_loss_degrades_but_keeps_history(project: Path, service_factory) -> None:
    (project / "large_train.py").write_text(
        """import json, os
from pathlib import Path
root = Path(os.environ['VERILAB_RUN_DIR'], 'outputs'); root.mkdir(parents=True)
root.joinpath('predictions.json').write_text(json.dumps({'case_ids':['a','b','c','d'],'predictions':[0,1,0,0]}))
root.joinpath('reported_metrics.json').write_text(json.dumps({'metrics':{'accuracy':0.75}}))
root.joinpath('checkpoint.bin').write_bytes(b'x' * (2 * 1024 * 1024))
""",
        encoding="utf-8",
    )
    commit_all(project, "add large artifact")
    policy = load_policy(
        project,
        small_artifact_limit_mib=1,
        required_artifact_roles=["predictions", "checkpoint"],
    )
    service = service_factory(policy=policy)
    expected = [
        {"role": "predictions", "glob": "outputs/predictions.json", "required": True},
        {
            "role": "reported_metrics",
            "glob": "outputs/reported_metrics.json",
            "required": True,
        },
        {"role": "checkpoint", "glob": "outputs/checkpoint.bin", "required": True},
    ]
    submitted = service.submit(
        make_spec(
            project,
            title="Large evidence",
            command=["python3", "large_train.py"],
            expected_artifacts=expected,
        )
    )
    service.run_next()
    assert service.get_experiment(submitted["experiment_id"])["status"] == "ACCEPTED"
    checkpoint = next(
        row
        for row in service._artifact_rows(submitted["experiment_id"])
        if row["role"] == "checkpoint"
    )
    assert checkpoint["object_path"] is None
    Path(checkpoint["absolute_path"]).unlink()
    service.refresh_evidence_health()
    result = service.get_experiment(submitted["experiment_id"])
    assert result["status"] == "ACCEPTED"
    assert result["evidence_health"] == "degraded"
    assert service.leaderboard()[0]["evidence_health"] == "degraded"
