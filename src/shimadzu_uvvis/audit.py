"""Atomic JSON records for commands and measurement manifests."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
        os.replace(temporary, destination)
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
