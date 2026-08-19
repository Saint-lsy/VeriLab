from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from .models import ProjectPolicy


def default_state_dir(project_id: str) -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "verilab" / project_id


@dataclass(frozen=True)
class Settings:
    project_root: Path
    state_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    heartbeat_seconds: float = 2.0
    capability_token: str = ""
    codex_binary: str = "codex"

    @classmethod
    def load(
        cls,
        *,
        project_root: str | Path | None = None,
        state_dir: str | Path | None = None,
        project_id: str | None = None,
    ) -> Settings:
        root = Path(project_root or os.environ.get("VERILAB_PROJECT_ROOT", ".")).resolve()
        guessed_id = project_id or re.sub(r"[^a-zA-Z0-9_.-]", "-", root.name)
        state = (
            Path(state_dir or os.environ.get("VERILAB_STATE_DIR", default_state_dir(guessed_id)))
            .expanduser()
            .resolve()
        )
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        token_path = state / "capability.token"
        if token_path.exists():
            token = token_path.read_text(encoding="utf-8").strip()
        else:
            token = secrets.token_urlsafe(32)
            token_path.write_text(token + "\n", encoding="utf-8")
            token_path.chmod(0o600)
        return cls(
            project_root=root,
            state_dir=state,
            host=os.environ.get("VERILAB_HOST", "127.0.0.1"),
            port=int(os.environ.get("VERILAB_PORT", "8765")),
            heartbeat_seconds=float(os.environ.get("VERILAB_HEARTBEAT_SECONDS", "2")),
            capability_token=token,
            codex_binary=os.environ.get("VERILAB_CODEX_BINARY", "codex"),
        )

    @property
    def database_path(self) -> Path:
        return self.state_dir / "state.sqlite3"

    @property
    def policy_path(self) -> Path:
        return self.state_dir / "policy.json"

    def ensure_layout(self) -> None:
        for name in (
            "objects/sha256",
            "runs",
            "reviewer-bundles",
            "worktrees",
            "codex",
            "policies",
        ):
            (self.state_dir / name).mkdir(parents=True, exist_ok=True)

    def load_policy(self, policy_hash: str | None = None) -> ProjectPolicy:
        path = (
            self.state_dir / "policies" / f"{policy_hash}.json" if policy_hash else self.policy_path
        )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError(f"trusted policy is not installed: {path}") from exc
        policy = ProjectPolicy.model_validate(value)
        if policy_hash and policy.policy_hash != policy_hash:
            raise RuntimeError(f"trusted policy snapshot hash mismatch: {path}")
        return policy

    def install_policy(self, policy: ProjectPolicy) -> Path:
        self.ensure_layout()
        snapshot = self.state_dir / "policies" / f"{policy.policy_hash}.json"
        serialized = policy.model_dump_json(indent=2) + "\n"
        if snapshot.exists():
            existing = ProjectPolicy.model_validate_json(snapshot.read_text(encoding="utf-8"))
            if existing.policy_hash != policy.policy_hash:
                raise RuntimeError(f"policy snapshot collision: {snapshot}")
        else:
            snapshot_temp = snapshot.with_suffix(".tmp")
            snapshot_temp.write_text(serialized, encoding="utf-8")
            snapshot_temp.chmod(0o600)
            snapshot_temp.replace(snapshot)
        temp = self.policy_path.with_suffix(".tmp")
        temp.write_text(serialized, encoding="utf-8")
        temp.chmod(0o600)
        temp.replace(self.policy_path)
        return self.policy_path
