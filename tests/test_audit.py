from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shimadzu_uvvis.audit import write_json_atomic


class AtomicJsonTests(unittest.TestCase):
    def test_transient_permission_error_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "manifest.json"
            attempts = 0
            real_replace = os.replace

            def replace_after_transient_failures(source: Path, target: Path) -> None:
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    error = PermissionError("simulated sharing violation")
                    error.winerror = 5  # type: ignore[attr-defined]
                    raise error
                real_replace(source, target)

            with (
                patch(
                    "shimadzu_uvvis.audit.os.replace",
                    side_effect=replace_after_transient_failures,
                ),
                patch("shimadzu_uvvis.audit.time.sleep") as sleep,
            ):
                write_json_atomic(destination, {"state": "WAITING_FOR_SAMPLE"})

            self.assertEqual(attempts, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(
                json.loads(destination.read_text(encoding="utf-8")),
                {"state": "WAITING_FOR_SAMPLE"},
            )

    def test_non_transient_permission_error_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "manifest.json"
            error = PermissionError("simulated permanent denial")
            error.winerror = 99  # type: ignore[attr-defined]

            with (
                patch("shimadzu_uvvis.audit.os.replace", side_effect=error) as replace,
                patch("shimadzu_uvvis.audit.time.sleep") as sleep,
            ):
                with self.assertRaises(PermissionError):
                    write_json_atomic(destination, {"state": "FAILED"})

            replace.assert_called_once()
            sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
