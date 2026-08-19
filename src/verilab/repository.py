from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .models import ExperimentSpec, ProjectPolicy, ensure_within
from .security import sha256_file


class RepositoryError(RuntimeError):
    pass


def git(
    root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        text=True,
        capture_output=True,
    )


def validate_submission_repository(root: Path, spec: ExperimentSpec) -> str:
    root = root.resolve()
    try:
        top = Path(git(root, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    except subprocess.CalledProcessError as exc:
        raise RepositoryError("experiment project must be a Git repository") from exc
    if top != root:
        raise RepositoryError(f"project root must be Git top-level: {top}")
    dirty = git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout.strip()
    if dirty:
        raise RepositoryError("experiment project must have a clean working tree")
    try:
        commit = git(root, "rev-parse", "--verify", f"{spec.git_commit}^{{commit}}").stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise RepositoryError(f"unknown commit: {spec.git_commit}") from exc
    if not git(root, "merge-base", "--is-ancestor", commit, "HEAD", check=False).returncode == 0:
        raise RepositoryError("submitted commit is not reachable from HEAD")
    ensure_within(root, spec.cwd)
    return commit


def create_detached_worktree(root: Path, destination: Path, commit: str) -> Path:
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        head = git(destination, "rev-parse", "HEAD", check=False)
        if head.returncode == 0 and head.stdout.strip() == commit:
            return destination
        raise RepositoryError(f"worktree destination is not empty: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        git(root, "worktree", "add", "--detach", str(destination), commit)
    except subprocess.CalledProcessError as exc:
        raise RepositoryError(exc.stderr.strip() or "failed to create detached worktree") from exc
    actual = git(destination, "rev-parse", "HEAD").stdout.strip()
    if actual != commit:
        raise RepositoryError("detached worktree commit mismatch")
    return destination


def pin_policy_code(policy: ProjectPolicy, project_root: Path) -> ProjectPolicy:
    hashes: dict[str, str] = {}
    for raw in policy.grader_code_paths:
        expanded = raw.replace("{project_root}", str(project_root))
        path = Path(expanded).expanduser().resolve()
        if not path.is_file():
            raise RepositoryError(f"grader code path is not a file: {path}")
        hashes[str(path)] = sha256_file(path)
    metadata = dict(policy.metadata)
    metadata["grader_code_sha256"] = hashes
    return policy.model_copy(update={"metadata": metadata})


def verify_policy_code(policy: ProjectPolicy) -> list[str]:
    errors: list[str] = []
    expected = policy.metadata.get("grader_code_sha256", {})
    if not isinstance(expected, dict):
        return ["policy grader_code_sha256 is malformed"]
    for raw_path, digest in expected.items():
        path = Path(raw_path)
        if not path.is_file():
            errors.append(f"trusted grader code is missing: {path}")
        elif sha256_file(path) != digest:
            errors.append(f"trusted grader code hash drift: {path}")
    return errors


def policy_public_view(policy: ProjectPolicy) -> dict[str, object]:
    value = policy.model_dump(mode="json")
    value["grader_command"] = ["[controller-private]"]
    value["secret_names"] = ["[redacted]"] * len(policy.secret_names)
    metadata = dict(value.get("metadata", {}))
    code_hashes = metadata.pop("grader_code_sha256", {})
    metadata["grader_code_sha256"] = sorted(code_hashes.values())
    value["metadata"] = metadata
    value["policy_hash"] = policy.policy_hash
    value["comparison_key"] = policy.comparison_key
    return json.loads(json.dumps(value))
