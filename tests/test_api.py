from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from verilab.api import create_app
from verilab.codex_driver import CodexResult

from .conftest import make_spec


def _event_ids(body: str) -> list[int]:
    return [int(line[4:]) for line in body.splitlines() if line.startswith("id: ")]


def test_dashboard_csrf_sse_replay_and_bundle(project: Path, service_factory) -> None:
    service = service_factory()
    submitted = service.submit(make_spec(project, title="API complete flow"))
    service.run_next()
    app = create_app(service, settings=service.settings, start_worker=False)
    with TestClient(app) as client:
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "Evidence before rank" in dashboard.text
        assert "API complete flow" in dashboard.text
        detail = client.get(f"/experiments/{submitted['experiment_id']}")
        assert detail.status_code == 200
        assert "Metric provenance" in detail.text

        blocked = client.post(
            f"/api/experiments/{submitted['experiment_id']}/notes", json={"note": "x"}
        )
        assert blocked.status_code == 403
        csrf = client.cookies["verilab_csrf"]
        allowed = client.post(
            f"/api/experiments/{submitted['experiment_id']}/notes",
            json={"note": "audited note"},
            headers={"X-CSRF-Token": csrf},
        )
        assert allowed.status_code == 200

        first = client.get("/api/events?after=0&once=true")
        ids = _event_ids(first.text)
        assert ids == sorted(set(ids))
        last = ids[-1]
        client.post(
            f"/api/experiments/{submitted['experiment_id']}/notes",
            json={"note": "one more event"},
            headers={"X-CSRF-Token": csrf},
        )
        replay = client.get("/api/events?after=0&once=true", headers={"Last-Event-ID": str(last)})
        replay_ids = _event_ids(replay.text)
        assert replay_ids
        assert min(replay_ids) > last

        bundle = client.get(f"/api/experiments/{submitted['experiment_id']}/bundle")
        assert bundle.status_code == 200
        assert bundle.headers["content-type"] == "application/zip"
        assert bundle.content.startswith(b"PK")


def test_capability_submit_is_idempotent(project: Path, service_factory) -> None:
    service = service_factory()
    app = create_app(service, settings=service.settings, start_worker=False)
    spec = make_spec(project, title="API idempotency").model_dump(mode="json")
    headers = {"Authorization": f"Bearer {service.settings.capability_token}"}
    with TestClient(app) as client:
        unauthorized = client.post("/api/experiments", json=spec)
        assert unauthorized.status_code in {401, 403}
        first = client.post("/api/experiments", json=spec, headers=headers)
        second = client.post("/api/experiments", json=spec, headers=headers)
        assert first.status_code == second.status_code == 200
        assert first.json()["run_id"] == second.json()["run_id"]
        assert second.json()["deduplicated"] is True
        cannot_withdraw = client.post(
            f"/api/experiments/{first.json()['experiment_id']}/withdraw",
            json={"reason": "agent must not have this capability"},
            headers=headers,
        )
        assert cannot_withdraw.status_code == 403


def test_web_language_switch_is_persistent_and_covers_structured_views(
    project: Path, service_factory
) -> None:
    service = service_factory()
    submitted = service.submit(make_spec(project, title="Bilingual audit flow"))
    service.run_next()
    app = create_app(service, settings=service.settings, start_worker=False)

    with TestClient(app) as client:
        switch = client.get(
            f"/language/zh?next=/experiments/{submitted['experiment_id']}",
            follow_redirects=False,
        )
        assert switch.status_code == 303
        assert switch.headers["location"] == f"/experiments/{submitted['experiment_id']}"
        assert client.cookies["verilab_language"] == "zh"

        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert '<html lang="zh-CN">' in dashboard.text
        assert "证据先于排名" in dashboard.text
        assert "排行榜" in dashboard.text
        assert "验证分数" in dashboard.text
        assert "已通过" in dashboard.text
        assert "/language/en?next=/" in dashboard.text

        detail = client.get(f"/experiments/{submitted['experiment_id']}")
        assert detail.status_code == 200
        assert "冻结的执行信息" in detail.text
        assert "指标来源" in detail.text
        assert "准确率" in detail.text
        assert "产物清单" in detail.text
        assert "独立审核" in detail.text
        assert "授权执行" in detail.text
        assert "实验已提交" in detail.text

        audit = client.get("/audit")
        assert audit.status_code == 200
        assert "审计收件箱" in audit.text
        assert "运行完整性检查" in audit.text

        unsafe_redirect = client.get(
            "/language/en?next=//example.test",
            follow_redirects=False,
        )
        assert unsafe_redirect.status_code == 303
        assert unsafe_redirect.headers["location"] == "/"
        assert client.get("/").text.find("Evidence before rank") >= 0

        unsupported = client.get("/language/fr", follow_redirects=False)
        assert unsupported.status_code == 404


def test_review_bundle_contains_auditable_contract(project: Path, service_factory) -> None:
    service = service_factory()
    submitted = service.submit(make_spec(project, title="Bundle contract"))
    service.run_next()
    directory = service.latest_review_bundle(submitted["experiment_id"])
    expected = {
        "experiment.json",
        "run.json",
        "policy.public.json",
        "artifacts.json",
        "metrics.json",
        "events.jsonl",
        "bundle-manifest.json",
        "bundle.sha256",
        "review-output.json",
    }
    assert expected.issubset({path.name for path in directory.iterdir()})
    manifest = json.loads((directory / "bundle-manifest.json").read_text(encoding="utf-8"))
    assert manifest["target_bundle_sha256"] == (directory / "bundle.sha256").read_text().strip()
    policy = json.loads((directory / "policy.public.json").read_text(encoding="utf-8"))
    assert policy["grader_command"] == ["[controller-private]"]


def test_dummy_conversation_to_verified_bundle(project: Path, service_factory) -> None:
    service = service_factory()
    spec = make_spec(project, title="Conversation-submitted experiment")

    class ExecutorDouble:
        def executor(self, **_kwargs):
            service.submit(spec)
            return CodexResult(
                thread_id="executor-thread-1",
                final_text="Submitted the frozen experiment and queued its formal run.",
                events=[{"type": "thread.started", "thread_id": "executor-thread-1"}],
            )

    service.codex_driver = ExecutorDouble()  # type: ignore[assignment]
    response = service.chat("Run the deterministic dummy hypothesis through VeriLab.")
    assert response["thread_id"] == "executor-thread-1"
    assert service.run_next() is True
    experiments = service.list_experiments()
    assert len(experiments) == 1
    result = service.get_experiment(experiments[0]["id"])
    assert result["status"] == "ACCEPTED"
    assert service.leaderboard()[0]["score"] == 0.75
    assert service.latest_review_bundle(result["id"]).is_dir()
    with service.db.connect() as connection:
        roles = [row[0] for row in connection.execute("SELECT role FROM codex_sessions")]
        message_roles = [row[0] for row in connection.execute("SELECT role FROM messages")]
    assert roles.count("executor") == 1
    assert roles.count("reviewer") == 1
    assert message_roles == ["user", "assistant"]
