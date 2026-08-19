from __future__ import annotations

import glob
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import ExperimentSpec, ProjectPolicy
from .security import sha256_file


class ArtifactError(RuntimeError):
    pass


@dataclass(frozen=True)
class SealedArtifact:
    id: str
    role: str
    relative_path: str
    absolute_path: str
    sha256: str
    size_bytes: int
    required: bool
    object_path: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "relative_path": self.relative_path,
            "absolute_path": self.absolute_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "required": self.required,
            "object_path": self.object_path,
        }


class ArtifactStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.object_root = state_dir / "objects" / "sha256"
        self.object_root.mkdir(parents=True, exist_ok=True)

    def _store_object(self, source: Path, digest: str) -> Path:
        target = self.object_root / digest[:2] / digest[2:]
        if target.exists():
            if sha256_file(target) != digest:
                raise ArtifactError(f"content-addressed object is corrupt: {target}")
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        shutil.copyfile(source, temp)
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        if sha256_file(temp) != digest:
            temp.unlink(missing_ok=True)
            raise ArtifactError(f"artifact changed while sealing: {source}")
        temp.replace(target)
        target.chmod(0o444)
        return target

    def seal(
        self,
        *,
        run_dir: Path,
        spec: ExperimentSpec,
        policy: ProjectPolicy,
    ) -> list[SealedArtifact]:
        root = run_dir.resolve()
        artifacts: list[SealedArtifact] = []
        seen: set[Path] = set()
        limit = policy.small_artifact_limit_mib * 1024 * 1024
        expected_roles = {item.role for item in spec.expected_artifacts}
        missing_policy_roles = set(policy.required_artifact_roles) - expected_roles
        if missing_policy_roles:
            raise ArtifactError(
                f"spec does not declare policy-required roles: {sorted(missing_policy_roles)}"
            )
        for expectation in spec.expected_artifacts:
            matches = sorted(
                Path(item).resolve()
                for item in glob.glob(str(root / expectation.glob), recursive=True)
            )
            valid: list[Path] = []
            for path in matches:
                if path in seen or not path.is_file():
                    continue
                if root not in path.parents:
                    raise ArtifactError(f"artifact escaped run directory: {path}")
                valid.append(path)
                seen.add(path)
            if expectation.required and not valid:
                raise ArtifactError(f"required artifact role {expectation.role!r} matched no files")
            for path in valid:
                stat_before = path.stat()
                digest = sha256_file(path)
                stat_after = path.stat()
                if (stat_before.st_size, stat_before.st_mtime_ns) != (
                    stat_after.st_size,
                    stat_after.st_mtime_ns,
                ):
                    raise ArtifactError(f"artifact changed while hashing: {path}")
                object_path = None
                if stat_after.st_size <= limit:
                    object_path = str(self._store_object(path, digest))
                artifacts.append(
                    SealedArtifact(
                        id=f"art_{uuid.uuid4().hex}",
                        role=expectation.role,
                        relative_path=str(path.relative_to(root)),
                        absolute_path=str(path),
                        sha256=digest,
                        size_bytes=stat_after.st_size,
                        required=expectation.required,
                        object_path=object_path,
                    )
                )
        return artifacts

    def seal_file(
        self,
        *,
        run_dir: Path,
        path: Path,
        role: str,
        required: bool,
        limit_bytes: int,
    ) -> SealedArtifact:
        root = run_dir.resolve()
        source = path.resolve()
        if not source.is_file() or root not in source.parents:
            raise ArtifactError(f"artifact is not a file inside run directory: {source}")
        stat_before = source.stat()
        digest = sha256_file(source)
        stat_after = source.stat()
        if (stat_before.st_size, stat_before.st_mtime_ns) != (
            stat_after.st_size,
            stat_after.st_mtime_ns,
        ):
            raise ArtifactError(f"artifact changed while hashing: {source}")
        object_path = None
        if stat_after.st_size <= limit_bytes:
            object_path = str(self._store_object(source, digest))
        return SealedArtifact(
            id=f"art_{uuid.uuid4().hex}",
            role=role,
            relative_path=str(source.relative_to(root)),
            absolute_path=str(source),
            sha256=digest,
            size_bytes=stat_after.st_size,
            required=required,
            object_path=object_path,
        )

    @staticmethod
    def manifest(artifacts: list[SealedArtifact]) -> dict[str, object]:
        return {
            "schema_version": 1,
            "artifacts": [item.to_dict() for item in artifacts],
        }

    @staticmethod
    def write_manifest(path: Path, artifacts: list[SealedArtifact]) -> None:
        path.write_text(
            json.dumps(ArtifactStore.manifest(artifacts), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def verify_record(record: dict[str, Any], *, pre_review: bool) -> tuple[bool, str]:
        source = Path(record["absolute_path"])
        obj = Path(record["object_path"]) if record.get("object_path") else None
        if pre_review:
            if not source.is_file():
                return False, "missing"
            return (
                (True, "healthy")
                if sha256_file(source) == record["sha256"]
                else (False, "hash_drift")
            )
        candidates = [candidate for candidate in (obj, source) if candidate is not None]
        for candidate in candidates:
            if candidate.is_file() and sha256_file(candidate) == record["sha256"]:
                return True, "healthy"
        if any(candidate.exists() for candidate in candidates):
            return False, "hash_drift"
        return False, "missing"
