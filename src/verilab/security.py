from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from pathlib import Path

SECRET_KEY_PATTERN = re.compile(r"(TOKEN|PASSWORD|PASSWD|SECRET|API_KEY|PRIVATE_KEY)", re.I)
BEARER_PATTERN = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def redact_text(value: str, secrets: Iterable[str] = ()) -> str:
    result = BEARER_PATTERN.sub(r"\1[REDACTED]", value)
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        result = result.replace(secret, "[REDACTED]")
    return result


def redacted_environment(
    environment: dict[str, str], secret_names: Iterable[str]
) -> dict[str, str]:
    explicit = set(secret_names)
    return {
        key: "[REDACTED]" if key in explicit or SECRET_KEY_PATTERN.search(key) else value
        for key, value in environment.items()
    }


def sanitized_process_environment(
    environment: dict[str, str] | None = None,
) -> dict[str, str]:
    source = environment or dict(os.environ)
    return {
        key: value
        for key, value in source.items()
        if not SECRET_KEY_PATTERN.search(key)
        and not key.startswith("OPENAI_")
        and key != "CODEX_API_KEY"
    }


def process_start_ticks(pid: int) -> int | None:
    try:
        # /proc/<pid>/stat field 22; the command name may contain spaces and parentheses.
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        tail = stat[stat.rfind(")") + 2 :].split()
        return int(tail[19])
    except (FileNotFoundError, PermissionError, ValueError, IndexError):
        return None


def process_cpu_ticks(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        tail = stat[stat.rfind(")") + 2 :].split()
        return int(tail[11]) + int(tail[12])
    except (FileNotFoundError, PermissionError, ValueError, IndexError):
        return None


def same_process(pid: int, start_ticks: int | None) -> bool:
    if pid <= 0 or start_ticks is None:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return process_start_ticks(pid) == start_ticks
