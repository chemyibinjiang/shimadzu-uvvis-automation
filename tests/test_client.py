from __future__ import annotations

import os
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from shimadzu_uvvis.client import (
    LabSolutionsBusyError,
    LabSolutionsClient,
    LabSolutionsCommandError,
    LabSolutionsRecoveryRequiredError,
    LabSolutionsTimeoutError,
    parse_exchange_text,
)


class LabSolutionsClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.client = LabSolutionsClient(
            self.root, timeout=2.0, poll_interval=0.01
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _respond_once(
        self, *, return_code: int = 0, error: str = ""
    ) -> tuple[threading.Thread, dict[str, str]]:
        captured: dict[str, str] = {}

        def responder() -> None:
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if self.client.command_path.exists():
                    captured.update(
                        parse_exchange_text(
                            self.client.command_path.read_text(encoding="utf-8")
                        )
                    )
                    self.client.command_path.unlink()
                    command = captured["Command"]
                    self.client.feedback_path.write_text(
                        f'Command={command}\r\nReturn={return_code}\r\n'
                        f'Error="{error}"\r\n',
                        encoding="utf-8",
                    )
                    return
                time.sleep(0.005)

        thread = threading.Thread(target=responder, daemon=True)
        thread.start()
        return thread, captured

    def test_send_command_and_parse_feedback(self) -> None:
        thread, captured = self._respond_once()

        result = self.client.send_command(
            100,
            ParameterFileName=Path(r"C:\UVVis-Data\Parameter\scan.vspm"),
        )
        thread.join(timeout=1.0)

        self.assertTrue(result.ok)
        self.assertEqual(result.command, 100)
        self.assertEqual(captured["Command"], "100")
        self.assertEqual(
            captured["ParameterFileName"],
            r"C:\UVVis-Data\Parameter\scan.vspm",
        )
        self.assertFalse(self.client.recovery_path.exists())

    def test_nonzero_return_raises_command_error(self) -> None:
        thread, _ = self._respond_once(return_code=-3200, error="Measurement error")

        with self.assertRaises(LabSolutionsCommandError) as context:
            self.client.send_command(111, MeasurementMode=1)
        thread.join(timeout=1.0)

        self.assertEqual(context.exception.feedback.return_code, -3200)
        self.assertIn("Measurement error", str(context.exception))

    def test_wait_for_export_requires_stable_file(self) -> None:
        export_dir = self.root / "export"
        export_dir.mkdir()
        export_path = export_dir / "run_001.csv"
        started_at = time.time()

        def writer() -> None:
            time.sleep(0.03)
            export_path.write_text("wavelength,absorbance\n", encoding="utf-8")
            time.sleep(0.03)
            with export_path.open("a", encoding="utf-8") as handle:
                handle.write("500,0.42\n")
                handle.flush()
                os.fsync(handle.fileno())

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        result = self.client.wait_for_export(
            export_dir,
            since=started_at,
            timeout=1.0,
            stable_seconds=0.05,
        )
        thread.join(timeout=1.0)

        self.assertEqual(result, export_path)
        self.assertIn("500,0.42", result.read_text(encoding="utf-8"))

    def test_spectrum_workflow_uses_expected_command_order(self) -> None:
        export_dir = self.root / "export"
        export_dir.mkdir()
        commands: list[int] = []
        measurement_fields: dict[str, str] = {}

        def responder() -> None:
            while len(commands) < 4:
                if not self.client.command_path.exists():
                    time.sleep(0.005)
                    continue
                fields = parse_exchange_text(
                    self.client.command_path.read_text(encoding="utf-8")
                )
                command = int(fields["Command"])
                commands.append(command)
                if command == 111:
                    measurement_fields.update(fields)
                self.client.command_path.unlink()
                if command == 111:
                    (export_dir / "run_001.csv").write_text(
                        "wavelength,absorbance\n500,0.42\n", encoding="utf-8"
                    )
                self.client.feedback_path.write_text(
                    f'Command={command}\r\nReturn=0\r\nError=""\r\n',
                    encoding="utf-8",
                )

        thread = threading.Thread(target=responder, daemon=True)
        thread.start()
        result = self.client.run_spectrum(
            method_file=r"C:\UVVis-Data\Parameter\scan.vspm",
            sample_name="sample_001",
            sample_id="run_001",
            export_dir=export_dir,
            stable_seconds=0.02,
        )
        thread.join(timeout=1.0)

        self.assertEqual(commands, [0, 100, 110, 111])
        self.assertEqual(measurement_fields["MeasurementMode"], "2")
        self.assertEqual(measurement_fields["Discharge"], "OFF")
        self.assertEqual(result.export_path, export_dir / "run_001.csv")

    def test_two_clients_cannot_send_at_the_same_time(self) -> None:
        first = LabSolutionsClient(
            self.root, timeout=1.0, poll_interval=0.01, lock_timeout=0.5
        )
        second = LabSolutionsClient(
            self.root, timeout=1.0, poll_interval=0.01, lock_timeout=0.05
        )
        outcome: list[object] = []

        def first_sender() -> None:
            try:
                outcome.append(first.send_command(0))
            except Exception as exc:  # pragma: no cover - assertion reports the error
                outcome.append(exc)

        thread = threading.Thread(target=first_sender, daemon=True)
        thread.start()
        deadline = time.monotonic() + 0.5
        while not first.command_path.exists() and time.monotonic() < deadline:
            time.sleep(0.005)

        with self.assertRaises(LabSolutionsBusyError):
            second.send_command(0)

        first.command_path.unlink()
        first.feedback_path.write_text(
            'Command=0\r\nReturn=0\r\nError=""\r\n', encoding="utf-8"
        )
        thread.join(timeout=1.0)
        self.assertEqual(len(outcome), 1)
        self.assertFalse(isinstance(outcome[0], Exception))

    def test_command_audit_records_labsolutions_error(self) -> None:
        audit_dir = self.root / "audit"
        self.client = LabSolutionsClient(
            self.root,
            timeout=1.0,
            poll_interval=0.01,
            audit_dir=audit_dir,
        )
        thread, _ = self._respond_once(return_code=-3200, error="Measurement error")

        with self.assertRaises(LabSolutionsCommandError):
            self.client.send_command(111, MeasurementMode=2, Discharge=False)
        thread.join(timeout=1.0)

        records = list(audit_dir.rglob("*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "labsolutions_error")
        self.assertTrue(record["command_written"])
        self.assertEqual(record["feedback"]["return_code"], -3200)

    def test_timeout_blocks_commands_until_matching_feedback_is_acknowledged(self) -> None:
        client = LabSolutionsClient(
            self.root, timeout=0.05, poll_interval=0.01, lock_timeout=0.1
        )

        with self.assertRaises(LabSolutionsTimeoutError):
            client.send_command(0)

        self.assertTrue(client.recovery_path.exists())
        with self.assertRaises(LabSolutionsRecoveryRequiredError):
            client.send_command(0)

        client.command_path.unlink()
        client.feedback_path.write_text(
            'Command=0\r\nReturn=0\r\nError=""\r\n', encoding="utf-8"
        )
        snapshot = client.clear_recovery()

        self.assertFalse(snapshot["recovery_required"])
        self.assertFalse(client.recovery_path.exists())


if __name__ == "__main__":
    unittest.main()
