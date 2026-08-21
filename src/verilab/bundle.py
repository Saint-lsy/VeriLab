from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

from .models import canonical_sha256
from .security import sha256_file


class AuditBundle:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def build(
        self,
        *,
        experiment: dict[str, Any],
        run: dict[str, Any],
        policy: dict[str, Any],
        change_context: dict[str, Any],
        artifacts: list[dict[str, Any]],
        metrics: list[dict[str, Any]],
        events: list[dict[str, Any]],
    ) -> tuple[str, set[str], set[str]]:
        self.directory.mkdir(parents=True, exist_ok=False)
        files: dict[str, object] = {
            "experiment.json": experiment,
            "run.json": run,
            "policy.public.json": policy,
            "change-context.json": change_context,
            "artifacts.json": {"schema_version": 1, "artifacts": artifacts},
            "metrics.json": {"schema_version": 1, "metrics": metrics},
        }
        for name, value in files.items():
            (self.directory / name).write_text(
                json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        event_path = self.directory / "events.jsonl"
        event_path.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
            encoding="utf-8",
        )
        inventory = []
        allowed_sha_refs: set[str] = set()
        for path in sorted(self.directory.iterdir()):
            if not path.is_file():
                continue
            digest = sha256_file(path)
            inventory.append(
                {"path": path.name, "sha256": digest, "size_bytes": path.stat().st_size}
            )
            allowed_sha_refs.add(f"sha256:{digest}")
        for artifact in artifacts:
            allowed_sha_refs.add(f"sha256:{artifact['sha256']}")
        bundle_sha = canonical_sha256({"schema_version": 1, "files": inventory})
        # The target digest is recorded in bundle-manifest.json, and the
        # reviewer contract permits SHA references copied from that manifest.
        allowed_sha_refs.add(f"sha256:{bundle_sha}")
        manifest = {
            "schema_version": 1,
            "target_bundle_sha256": bundle_sha,
            "files": inventory,
        }
        (self.directory / "bundle-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (self.directory / "bundle.sha256").write_text(bundle_sha + "\n", encoding="utf-8")
        allowed_event_refs = {f"event:{event['seq']}" for event in events}
        return bundle_sha, allowed_event_refs, allowed_sha_refs

    @staticmethod
    def export(directory: Path) -> bytes:
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            inventory = []
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    relative = str(path.relative_to(directory))
                    archive.write(path, relative)
                    inventory.append(
                        {
                            "path": relative,
                            "sha256": sha256_file(path),
                            "size_bytes": path.stat().st_size,
                        }
                    )
            archive.writestr(
                "EXPORT-MANIFEST.json",
                json.dumps({"schema_version": 1, "files": inventory}, indent=2, sort_keys=True)
                + "\n",
            )
        return buffer.getvalue()


def reviewer_prompt(bundle_dir: Path, bundle_sha256: str) -> str:
    return f"""You are a fresh, independent experiment-result Reviewer.
You did not participate in proposing or executing this experiment. Read the frozen code in your
read-only workspace and every evidence file in {bundle_dir}. Decide only whether this completed
result is valid and eligible for its comparison-key leaderboard. A real low score is eligible.

The exact target bundle SHA256 is: {bundle_sha256}

Return only the JSON object required by the provided schema. Include each required check exactly
once: authorized_execution, execution_evidence, artifact_integrity, metric_reproducibility,
protocol_compliance, data_and_split_integrity, result_consistency,
required_artifacts_complete. Every evidence_refs item must be copied from an actual event:<seq>
reference in events.jsonl or sha256:<digest> from bundle-manifest.json/artifacts.json. Use status
unknown when evidence is insufficient and never infer missing evidence. Verdict eligible requires
all eight checks to pass; use ineligible for a demonstrated failure and needs_human only for an
issue that cannot be decided from this read-only bundle.

Before an eligible result can enter the leaderboard, also write change_summary as a concise,
human-readable Simplified Chinese explanation grounded in change-context.json. Explain what changed
relative to the declared parent, why the change was expected to help, and what the trusted metric
actually showed. Call a change an improvement only when the observed evidence supports that claim;
state regressions and inconclusive results plainly. For a root experiment, describe it as the
baseline rather than inventing a parent comparison. Keep key_changes focused on scientific choices,
not raw file lists. Every change_summary.evidence_refs item must use an allowed event or SHA256
reference from the bundle.
"""
