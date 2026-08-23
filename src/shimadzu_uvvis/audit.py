"""Atomic JSON records for commands and measurement manifests."""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


_ATOMIC_REPLACE_DELAYS_SECONDS = (0.05, 0.1, 0.2, 0.4, 0.8)
_TRANSIENT_WINDOWS_REPLACE_ERRORS = {5, 32, 33}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _replace_with_retry(source: Path, destination: Path) -> None:
    for attempt, delay_seconds in enumerate(
        (*_ATOMIC_REPLACE_DELAYS_SECONDS, None)
    ):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            winerror = getattr(exc, "winerror", None)
            if (
                attempt >= len(_ATOMIC_REPLACE_DELAYS_SECONDS)
                or (winerror is not None and winerror not in _TRANSIENT_WINDOWS_REPLACE_ERRORS)
            ):
                raise
            time.sleep(delay_seconds)


def write_json_atomic(path: str | Path, payload: Mapping[str, Any]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="",
        )
        _replace_with_retry(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


class AuditRecorder:
    """Write one immutable transaction file per command."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        probe = self.directory / f".write_probe_{uuid.uuid4().hex}.tmp"
        try:
            probe.write_text("ok", encoding="ascii")
        finally:
            probe.unlink(missing_ok=True)

    def record(self, payload: Mapping[str, Any]) -> Path:
        now = utc_now()
        request_id = str(payload.get("request_id", uuid.uuid4().hex))
        command = payload.get("command", "unknown")
        timestamp = now.strftime("%Y%m%dT%H%M%S_%fZ")
        filename = f"{timestamp}_cmd{command}_{request_id}.json"
        return write_json_atomic(
            self.directory / now.strftime("%Y-%m-%d") / filename,
            payload,
        )
