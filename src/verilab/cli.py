from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import uvicorn

from .api import create_app
from .config import Settings
from .models import ExperimentSpec, ProjectPolicy
from .repository import pin_policy_code
from .service import VeriLabService


def _api_url(args: argparse.Namespace) -> str:
    return (
        getattr(args, "api_url", None)
        or os.environ.get("VERILAB_API_URL")
        or "http://127.0.0.1:8765"
    ).rstrip("/")


def _token(args: argparse.Namespace) -> str:
    path = getattr(args, "capability_file", None) or os.environ.get("VERILAB_CAPABILITY_FILE")
    if not path:
        raise SystemExit("VERILAB_CAPABILITY_FILE is required for Controller mutations")
    return Path(path).read_text(encoding="utf-8").strip()


def _request(
    args: argparse.Namespace,
    method: str,
    path: str,
    body: object | None = None,
    *,
    authorized: bool = False,
) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if authorized:
        headers["Authorization"] = f"Bearer {_token(args)}"
    request = urllib.request.Request(
        _api_url(args) + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"Controller returned HTTP {exc.code}: {detail}") from exc


def cmd_submit(args: argparse.Namespace) -> int:
    value = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    spec = ExperimentSpec.model_validate(value)
    print(
        json.dumps(
            _request(
                args, "POST", "/api/experiments", spec.model_dump(mode="json"), authorized=True
            ),
            indent=2,
        )
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    path = f"/api/experiments/{args.experiment_id}" if args.experiment_id else "/api/experiments"
    print(json.dumps(_request(args, "GET", path), indent=2))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    print(
        json.dumps(
            _request(args, "POST", f"/api/runs/{args.run_id}/cancel", {}, authorized=True),
            indent=2,
        )
    )
    return 0


def cmd_follow(args: argparse.Namespace) -> int:
    request = urllib.request.Request(_api_url(args) + "/api/events?after=0")
    with urllib.request.urlopen(request, timeout=None) as response:
        for raw in response:
            line = raw.decode(errors="replace").rstrip()
            if line.startswith("data: "):
                event = json.loads(line[6:])
                if (
                    event["entity_id"] == args.run_id
                    or event["payload"].get("run_id") == args.run_id
                ):
                    print(json.dumps(event, ensure_ascii=False))
    return 0


def cmd_policy_install(args: argparse.Namespace) -> int:
    settings = Settings.load(project_root=args.project_root, state_dir=args.state_dir)
    policy = ProjectPolicy.model_validate_json(Path(args.policy).read_text(encoding="utf-8"))
    pinned = pin_policy_code(policy, settings.project_root)
    path = settings.install_policy(pinned)
    service = VeriLabService(settings)
    service.ledger.append(
        entity_type="policy",
        entity_id=pinned.project_id,
        event_type="policy.installed",
        actor="user",
        payload={
            "policy_hash": pinned.policy_hash,
            "comparison_key": pinned.comparison_key,
            "protocol_id": pinned.protocol_id,
        },
    )
    print(
        json.dumps(
            {
                "path": str(path),
                "policy_hash": pinned.policy_hash,
                "comparison_key": pinned.comparison_key,
            },
            indent=2,
        )
    )
    return 0


def cmd_audit_verify(args: argparse.Namespace) -> int:
    settings = Settings.load(project_root=args.project_root, state_dir=args.state_dir)
    result = VeriLabService(settings).audit_verify()
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def cmd_serve(args: argparse.Namespace) -> int:
    settings = Settings.load(project_root=args.project_root, state_dir=args.state_dir)
    app = create_app(settings=settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=args.log_level)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verilab")
    parser.add_argument("--version", action="version", version="verilab 0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)

    def client_options(command: argparse.ArgumentParser) -> None:
        command.add_argument("--api-url")
        command.add_argument("--capability-file")

    submit = sub.add_parser("submit", help="submit an immutable ExperimentSpec")
    submit.add_argument("spec")
    client_options(submit)
    submit.set_defaults(func=cmd_submit)

    status = sub.add_parser("status", help="show experiment status")
    status.add_argument("experiment_id", nargs="?")
    client_options(status)
    status.set_defaults(func=cmd_status)

    follow = sub.add_parser("follow", help="follow canonical events for a run")
    follow.add_argument("run_id")
    client_options(follow)
    follow.set_defaults(func=cmd_follow)

    cancel = sub.add_parser("cancel", help="cancel a queued or running run")
    cancel.add_argument("run_id")
    client_options(cancel)
    cancel.set_defaults(func=cmd_cancel)

    policy = sub.add_parser("policy", help="trusted Controller policy administration")
    policy_sub = policy.add_subparsers(dest="policy_command", required=True)
    install = policy_sub.add_parser("install")
    install.add_argument("policy")
    install.add_argument("--project-root", required=True)
    install.add_argument("--state-dir", required=True)
    install.set_defaults(func=cmd_policy_install)

    audit = sub.add_parser("audit", help="verify canonical chain and artifact health")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    verify = audit_sub.add_parser("verify")
    verify.add_argument("--project-root", required=True)
    verify.add_argument("--state-dir", required=True)
    verify.set_defaults(func=cmd_audit_verify)

    serve = sub.add_parser("serve", help="start the local Controller and web UI")
    serve.add_argument("--project-root", required=True)
    serve.add_argument("--state-dir")
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
