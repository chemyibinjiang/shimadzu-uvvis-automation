"""MCP-side guard for the gateway's persistent per-PC UV-Vis lease."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
import re
import tempfile
import time
from typing import Any


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())[:120] or "uvvis-01"


def _lock_path() -> Path:
    root = Path(os.getenv("AI_TUTOR_DATA_ROOT") or r"D:\AI-Tutor-Data")
    instrument = os.getenv("SHIMADZU_UVVIS_INSTRUMENT_ID") or f"{os.getenv('AI_TUTOR_NODE_ID', 'PC')}-SHIMADZU-UVVIS-01"
    return root / "runtime" / "instrument-locks" / f"{_safe(instrument)}.json"


@contextmanager
def _file_mutex(path: Path):
    marker = path.with_suffix(path.suffix + ".lock")
    deadline = time.time() + 15.0
    while True:
        try:
            marker.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            try:
                if time.time() - marker.stat().st_mtime > 30.0:
                    marker.rmdir()
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                raise RuntimeError("UV-Vis instrument lock file is busy")
            time.sleep(0.02)
    try:
        yield
    finally:
        try:
            marker.rmdir()
        except OSError:
            pass


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def guard_physical_action(*, student_id: str, session_id: str, batch_id: str = "") -> None:
    """Reject a physical action from a different student/session.

    The gateway normally creates the lease first.  The small fallback claim
    also protects direct MCP clients running on the instrument PC.
    """

    if str(os.getenv("SHIMADZU_UVVIS_ENFORCE_INSTRUMENT_LOCK", "false")).lower() not in {"1", "true", "yes", "on"}:
        return
    student_id = str(student_id or "").strip()
    session_id = str(session_id or "").strip()
    if not student_id or not session_id:
        raise RuntimeError("UV-Vis instrument lock requires student_id and session_id")
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_mutex(path):
        record = _read(path)
        state = str(record.get("state") or "FREE").upper()
        same = (
            str(record.get("student_id") or "").strip() == student_id
            and str(record.get("session_id") or "").strip() == session_id
        )
        if state != "FREE" and not same:
            owner = str(record.get("owner_label") or "另一组").strip()
            raise RuntimeError(f"UV-Vis 仪器正在由 {owner} 使用")
        if state != "FREE":
            return
        record.update(
            {
                "schema_version": 1,
                "instrument_id": os.getenv("SHIMADZU_UVVIS_INSTRUMENT_ID", ""),
                "node_id": os.getenv("AI_TUTOR_NODE_ID", ""),
                "state": "HELD",
                "student_id": student_id,
                "session_id": session_id,
                "batch_id": str(batch_id or "").strip(),
                "owner_label": student_id,
                "acquired_at": time.time(),
                "last_seen_at": time.time(),
                "results_persisted": False,
                "version": int(record.get("version") or 0) + 1,
            }
        )
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, indent=2)
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
