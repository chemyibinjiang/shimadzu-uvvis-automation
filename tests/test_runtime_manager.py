from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from shimadzu_uvvis.client import Feedback
from shimadzu_uvvis.configuration import load_settings
from shimadzu_uvvis.runtime_manager import (
    LabSolutionsRuntimeError,
    LabSolutionsRuntimeManager,
    SpectrumWindow,
    _MODE_TITLE_MARKERS,
)


class FakeRuntimeBackend:
    def __init__(
        self,
        command_dir: Path,
        *,
        waiting: bool = False,
        launched: bool = False,
    ) -> None:
        self.command_dir = command_dir
        self.status = "Automatic Control - Waiting" if waiting else None
        self.launched = launched
        self.calls: list[object] = []
        self.window = SpectrumWindow(1234, 5678, "Spectrum - [Analysis]")
        self.baseline_prompt = False

    def ensure_spectrum_window(self) -> tuple[SpectrumWindow, bool]:
        self.calls.append("ensure_window")
        return self.window, self.launched

    def waiting_status(self, window: SpectrumWindow) -> str | None:
        self.calls.append("waiting_status")
        return self.status

    def dismiss_parameter_change_baseline_prompt(
        self,
        window: SpectrumWindow,
        *,
        wait_seconds: float,
    ) -> bool:
        self.calls.append(("dismiss_baseline_prompt", wait_seconds))
        dismissed = self.baseline_prompt
        self.baseline_prompt = False
        return dismissed

    def leave_automatic_control(self, window: SpectrumWindow) -> None:
        self.calls.append("leave")
        self.status = None

    def ensure_command_directory(
        self,
        window: SpectrumWindow,
        expected: Path,
        *,
        allow_change: bool,
    ) -> bool:
        self.calls.append(("directory", allow_change))
        if self.command_dir == expected:
            return False
        if not allow_change:
            raise LabSolutionsRuntimeError("command directory mismatch")
        self.command_dir = expected
        return True

    def enter_automatic_control(self, window: SpectrumWindow) -> str:
        self.calls.append("enter")
        self.status = "Automatic Control - Waiting"
        return self.status


class FakeHelloClient:
    def __init__(self) -> None:
        self.calls: list[tuple[int, float | None]] = []

    def send_command(self, command: int, *, timeout: float | None = None) -> Feedback:
        self.calls.append((command, timeout))
        return Feedback(
            command=command,
            return_code=0,
            error="",
            fields=MappingProxyType(
                {"Command": str(command), "Return": "0", "Error": ""}
            ),
        )


class RuntimeManagerTests(unittest.TestCase):
    def test_spectrum_title_markers_include_installed_chinese_title(self) -> None:
        self.assertIn("光谱", _MODE_TITLE_MARKERS["spectrum"])

    def _settings(self, root: Path, *, enabled: bool = True):
        command = root / "control"
        command.mkdir()
        config = root / "control.toml"
        config.write_text(
            f"""
[labsolutions]
command_dir = "{command.as_posix()}"
mode = "spectrum"
poll_interval_seconds = 0.01
lock_timeout_seconds = 1.0

[runtime]
enabled = {str(enabled).lower()}
executable = "D:/UVNavi.exe"
arguments = ["/APP:Spectrum"]
startup_timeout_seconds = 2.0
ui_timeout_seconds = 2.0
ui_message_timeout_seconds = 1.0
hello_timeout_seconds = 3.0
configure_command_directory = true
""".strip(),
            encoding="utf-8",
        )
        return load_settings(config)

    def test_launch_configure_enter_and_hello_are_all_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = self._settings(root)
            backend = FakeRuntimeBackend(root / "old-control", launched=True)
            client = FakeHelloClient()
            manager = LabSolutionsRuntimeManager(
                settings,
                backend=backend,
                client_factory=lambda: client,  # type: ignore[arg-type]
            )

            ready = manager.ensure_ready(allow_reconfigure=True)

            self.assertEqual(ready.process_id, 1234)
            self.assertTrue(ready.launched)
            self.assertTrue(ready.command_directory_changed)
            self.assertEqual(ready.command_directory, settings.command_dir)
            self.assertEqual(client.calls, [(0, 3.0)])
            self.assertIn(("directory", True), backend.calls)
            self.assertIn("enter", backend.calls)

    def test_existing_waiting_state_still_requires_hello(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = self._settings(root)
            backend = FakeRuntimeBackend(settings.command_dir, waiting=True)
            client = FakeHelloClient()
            manager = LabSolutionsRuntimeManager(
                settings,
                backend=backend,
                client_factory=lambda: client,  # type: ignore[arg-type]
            )

            ready = manager.ensure_ready(allow_reconfigure=False)

            self.assertEqual(ready.waiting_status, "Automatic Control - Waiting")
            self.assertEqual(client.calls, [(0, 3.0)])
            self.assertNotIn("enter", backend.calls)
            self.assertFalse(
                any(
                    isinstance(call, tuple) and call[0] == "directory"
                    for call in backend.calls
                )
            )

    def test_existing_parameter_change_prompt_is_declined_before_hello(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = self._settings(root)
            backend = FakeRuntimeBackend(settings.command_dir, waiting=True)
            backend.baseline_prompt = True
            client = FakeHelloClient()
            manager = LabSolutionsRuntimeManager(
                settings,
                backend=backend,
                client_factory=lambda: client,  # type: ignore[arg-type]
            )

            manager.ensure_ready(allow_reconfigure=False)

            self.assertFalse(backend.baseline_prompt)
            dismiss_index = backend.calls.index(("dismiss_baseline_prompt", 0.0))
            waiting_index = backend.calls.index("waiting_status")
            self.assertLess(dismiss_index, waiting_index)

    def test_public_prompt_dismissal_uses_runtime_lock_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = self._settings(root)
            backend = FakeRuntimeBackend(settings.command_dir, waiting=True)
            backend.baseline_prompt = True
            manager = LabSolutionsRuntimeManager(
                settings,
                backend=backend,
                client_factory=FakeHelloClient,  # type: ignore[arg-type]
            )

            dismissed = manager.dismiss_parameter_change_baseline_prompt(
                wait_seconds=2.0
            )

            self.assertTrue(dismissed)
            self.assertIn(("dismiss_baseline_prompt", 2.0), backend.calls)

    def test_physical_phase_cannot_change_mismatched_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = self._settings(root)
            backend = FakeRuntimeBackend(root / "wrong-control")
            manager = LabSolutionsRuntimeManager(
                settings,
                backend=backend,
                client_factory=FakeHelloClient,  # type: ignore[arg-type]
            )

            with self.assertRaisesRegex(
                LabSolutionsRuntimeError, "command directory mismatch"
            ):
                manager.ensure_ready(allow_reconfigure=False)

            self.assertNotIn("enter", backend.calls)

    def test_disabled_runtime_blocks_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = self._settings(root, enabled=False)
            backend = FakeRuntimeBackend(settings.command_dir)
            manager = LabSolutionsRuntimeManager(
                settings,
                backend=backend,
                client_factory=FakeHelloClient,  # type: ignore[arg-type]
            )

            with self.assertRaisesRegex(LabSolutionsRuntimeError, "disabled"):
                manager.ensure_ready(allow_reconfigure=True)

            self.assertEqual(backend.calls, [])


if __name__ == "__main__":
    unittest.main()
