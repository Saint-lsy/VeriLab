from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import ReviewOutput
from .security import sanitized_process_environment


class CodexDriverError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodexResult:
    thread_id: str | None
    final_text: str
    events: list[dict[str, Any]]


class CodexDriver:
    """Small wrapper around the stable non-interactive Codex CLI surface."""

    def __init__(self, binary: str = "codex") -> None:
        self.binary = binary

    @staticmethod
    def _consume(process: subprocess.Popen[str], prompt: str, timeout: int) -> CodexResult:
        try:
            stdout, stderr = process.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            raise CodexDriverError(f"Codex timed out after {timeout} seconds") from exc
        events: list[dict[str, Any]] = []
        thread_id = None
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
                if event.get("type") == "thread.started":
                    thread_id = str(event.get("thread_id") or "") or None
        if process.returncode != 0:
            raise CodexDriverError(f"Codex exited {process.returncode}: {stderr[-4000:]}")
        return CodexResult(thread_id=thread_id, final_text="", events=events)

    def executor(
        self,
        *,
        prompt: str,
        cwd: Path,
        output_dir: Path,
        session_id: str | None = None,
        timeout: int = 1800,
        environment: dict[str, str] | None = None,
    ) -> CodexResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        final_path = output_dir / f"executor-{uuid.uuid4().hex}.txt"
        if session_id:
            command = [
                self.binary,
                "exec",
                "resume",
                session_id,
                "--json",
                "-o",
                str(final_path),
                "-",
            ]
        else:
            command = [
                self.binary,
                "exec",
                "--json",
                "--sandbox",
                "workspace-write",
                "-C",
                str(cwd),
                "-o",
                str(final_path),
                "-",
            ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=sanitized_process_environment(environment),
        )
        result = self._consume(process, prompt, timeout)
        final_text = final_path.read_text(encoding="utf-8") if final_path.exists() else ""
        return CodexResult(result.thread_id or session_id, final_text, result.events)

    def reviewer(
        self,
        *,
        prompt: str,
        cwd: Path,
        bundle_dir: Path,
        timeout: int,
    ) -> tuple[ReviewOutput, CodexResult]:
        schema_path = bundle_dir / "review-output.schema.json"
        output_path = bundle_dir / "review-output.json"
        jsonl_path = bundle_dir / "review-events.jsonl"
        schema_path.write_text(
            json.dumps(ReviewOutput.model_json_schema(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        command = [
            self.binary,
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            "-C",
            str(cwd),
            "--output-schema",
            str(schema_path),
            "-o",
            str(output_path),
            "-",
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=sanitized_process_environment(),
        )
        result = self._consume(process, prompt, timeout)
        jsonl_path.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in result.events),
            encoding="utf-8",
        )
        try:
            raw = json.loads(output_path.read_text(encoding="utf-8"))
            review = ReviewOutput.model_validate(raw)
        except Exception as exc:
            raise CodexDriverError("Codex reviewer output is missing or invalid") from exc
        final_text = output_path.read_text(encoding="utf-8")
        return review, CodexResult(result.thread_id, final_text, result.events)
