from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from shimadzu_uvvis.client import (
    LabSolutionsClient,
    LabSolutionsCommandError,
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
        self.assertEqual(result.export_path, export_dir / "run_001.csv")


if __name__ == "__main__":
    unittest.main()
