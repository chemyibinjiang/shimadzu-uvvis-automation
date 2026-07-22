"""Launch and readiness management for LabSolutions Spectrum on Windows."""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol

from .client import Feedback, LabSolutionsClient, LabSolutionsError, parse_exchange_text
from .configuration import ControlSettings, MeasurementMode
from .locking import FileLockTimeoutError, InterProcessFileLock


class LabSolutionsRuntimeError(LabSolutionsError):
    """Raised when a measurement program cannot reach a verified ready state."""


_MODE_APPLICATIONS: dict[MeasurementMode, str] = {
    "spectrum": "Spectrum",
    "photometric": "Photometric",
    "quantitation": "Quantitation",
    "time_course": "TimeCourse",
}
_MODE_TITLE_MARKERS: dict[MeasurementMode, tuple[str, ...]] = {
    "spectrum": ("光谱测定", "光谱", "spectrum"),
    "photometric": ("光度测定", "photometric"),
    "quantitation": ("定量测定", "quantitation"),
    "time_course": ("时间程序测定", "time course"),
}


def settings_for_mode(
    settings: ControlSettings, mode: MeasurementMode
) -> ControlSettings:
    """Derive mode-specific command files and UVNavi launch arguments."""

    application = _MODE_APPLICATIONS[mode]
    arguments = tuple(
        f"/APP:{application}" if value.upper().startswith("/APP:") else value
        for value in settings.runtime.arguments
    )
    if not any(value.upper().startswith("/APP:") for value in arguments):
        arguments = (f"/APP:{application}", *arguments)
    return replace(
        settings,
        mode=mode,
        runtime=replace(settings.runtime, arguments=arguments),
    )


@dataclass(frozen=True, slots=True)
class RuntimeReady:
    """Verified LabSolutions runtime state returned to a guarded workflow."""

    process_id: int
    window_handle: int
    launched: bool
    command_directory: Path
    command_directory_changed: bool
    waiting_status: str
    feedback: Feedback

    def as_dict(self) -> dict[str, object]:
        return {
            "state": "READY",
            "process_id": self.process_id,
            "window_handle": self.window_handle,
            "launched": self.launched,
            "command_directory": str(self.command_directory),
            "command_directory_changed": self.command_directory_changed,
            "waiting_status": self.waiting_status,
            "hello": {
                "command": self.feedback.command,
                "return_code": self.feedback.return_code,
                "error": self.feedback.error,
                "fields": dict(self.feedback.fields),
            },
        }


@dataclass(frozen=True, slots=True)
class SpectrumWindow:
    process_id: int
    handle: int
    title: str


class RuntimeUiBackend(Protocol):
    """UI boundary used by the runtime manager and replaced by fakes in tests."""

    def ensure_spectrum_window(self) -> tuple[SpectrumWindow, bool]: ...

    def waiting_status(self, window: SpectrumWindow) -> str | None: ...

    def dismiss_parameter_change_baseline_prompt(
        self,
        window: SpectrumWindow,
        *,
        wait_seconds: float,
    ) -> bool: ...

    def leave_automatic_control(self, window: SpectrumWindow) -> None: ...

    def ensure_command_directory(
        self,
        window: SpectrumWindow,
        expected: Path,
        *,
        allow_change: bool,
    ) -> bool: ...

    def enter_automatic_control(self, window: SpectrumWindow) -> str: ...


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _is_baseline_confirmation_text(value: str) -> bool:
    folded = value.casefold()
    return "基线校正" in value or "baseline correction" in folded


class WindowsLabSolutionsUi:
    """Deterministic Win32 control for the installed LabSolutions 1.13 UI."""

    _WM_COMMAND = 0x0111
    _WM_GETTEXT = 0x000D
    _WM_SETTEXT = 0x000C
    _WM_SETFOCUS = 0x0007
    _WM_KEYDOWN = 0x0100
    _WM_KEYUP = 0x0101
    _BM_CLICK = 0x00F5
    _VK_RIGHT = 0x27
    _TCM_GETITEMCOUNT = 0x1304
    _TCM_GETCURSEL = 0x130B
    _SMTO_ABORTIFHUNG = 0x0002
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def __init__(self, settings: ControlSettings) -> None:
        if os.name != "nt":
            raise LabSolutionsRuntimeError(
                "LabSolutions runtime automation is only supported on Windows"
            )
        from ctypes import wintypes

        self.settings = settings
        self.runtime = settings.runtime
        self._wintypes = wintypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        w = self._wintypes
        u = self._user32
        k = self._kernel32
        self._enum_callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        u.EnumWindows.argtypes = [self._enum_callback_type, w.LPARAM]
        u.EnumWindows.restype = w.BOOL
        u.EnumChildWindows.argtypes = [w.HWND, self._enum_callback_type, w.LPARAM]
        u.EnumChildWindows.restype = w.BOOL
        u.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        u.GetWindowThreadProcessId.restype = w.DWORD
        u.GetWindowTextLengthW.argtypes = [w.HWND]
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        u.GetDlgCtrlID.argtypes = [w.HWND]
        u.GetDlgCtrlID.restype = ctypes.c_int
        u.IsWindow.argtypes = [w.HWND]
        u.IsWindow.restype = w.BOOL
        u.IsWindowVisible.argtypes = [w.HWND]
        u.IsWindowVisible.restype = w.BOOL
        u.IsWindowEnabled.argtypes = [w.HWND]
        u.IsWindowEnabled.restype = w.BOOL
        u.GetMenu.argtypes = [w.HWND]
        u.GetMenu.restype = w.HMENU
        u.GetMenuItemCount.argtypes = [w.HMENU]
        u.GetMenuItemCount.restype = ctypes.c_int
        u.GetSubMenu.argtypes = [w.HMENU, ctypes.c_int]
        u.GetSubMenu.restype = w.HMENU
        u.GetMenuItemID.argtypes = [w.HMENU, ctypes.c_int]
        u.GetMenuItemID.restype = w.UINT
        u.GetMenuStringW.argtypes = [
            w.HMENU,
            w.UINT,
            w.LPWSTR,
            ctypes.c_int,
            w.UINT,
        ]
        u.GetMenuStringW.restype = ctypes.c_int
        u.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        u.PostMessageW.restype = w.BOOL
        u.SendMessageTimeoutW.argtypes = [
            w.HWND,
            w.UINT,
            w.WPARAM,
            w.LPARAM,
            w.UINT,
            w.UINT,
            ctypes.POINTER(w.LPARAM),
        ]
        u.SendMessageTimeoutW.restype = w.LPARAM
        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.OpenProcess.restype = w.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [
            w.HANDLE,
            w.DWORD,
            w.LPWSTR,
            ctypes.POINTER(w.DWORD),
        ]
        k.QueryFullProcessImageNameW.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL

    def _windows(self, *, process_id: int | None = None) -> list[int]:
        handles: list[int] = []
        w = self._wintypes

        @self._enum_callback_type
        def callback(handle: int, _lparam: int) -> bool:
            owner = w.DWORD()
            self._user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
            if process_id is None or owner.value == process_id:
                handles.append(int(handle))
            return True

        self._user32.EnumWindows(callback, 0)
        return handles

    def _children(self, parent: int) -> list[int]:
        handles: list[int] = []

        @self._enum_callback_type
        def callback(handle: int, _lparam: int) -> bool:
            handles.append(int(handle))
            return True

        self._user32.EnumChildWindows(parent, callback, 0)
        return handles

    def _window_text(self, handle: int) -> str:
        length = self._user32.GetWindowTextLengthW(handle)
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        self._user32.GetWindowTextW(handle, buffer, len(buffer))
        return buffer.value

    def _class_name(self, handle: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        self._user32.GetClassNameW(handle, buffer, len(buffer))
        return buffer.value

    def _process_path(self, process_id: int) -> Path | None:
        w = self._wintypes
        process = self._kernel32.OpenProcess(
            self._PROCESS_QUERY_LIMITED_INFORMATION, False, process_id
        )
        if not process:
            return None
        try:
            size = w.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not self._kernel32.QueryFullProcessImageNameW(
                process, 0, buffer, ctypes.byref(size)
            ):
                return None
            return Path(buffer.value)
        finally:
            self._kernel32.CloseHandle(process)

    @staticmethod
    def _normalize_menu_text(value: str) -> str:
        value = value.split("\t", 1)[0]
        value = re.sub(r"\(&.\)", "", value)
        return value.replace("&", "").rstrip(".\u2026 ").strip().casefold()

    def _menu_text(self, menu: int, position: int) -> str:
        buffer = ctypes.create_unicode_buffer(512)
        self._user32.GetMenuStringW(menu, position, buffer, len(buffer), 0x0400)
        return buffer.value

    def _find_menu_command(
        self,
        window: int,
        top_aliases: tuple[str, ...],
        item_aliases: tuple[str, ...],
    ) -> int:
        menu = self._user32.GetMenu(window)
        if not menu:
            raise LabSolutionsRuntimeError("Spectrum main menu was not found")
        normalized_top = {alias.casefold() for alias in top_aliases}
        normalized_items = {alias.casefold() for alias in item_aliases}
        for top_index in range(self._user32.GetMenuItemCount(menu)):
            top_text = self._normalize_menu_text(self._menu_text(menu, top_index))
            if top_text not in normalized_top:
                continue
            submenu = self._user32.GetSubMenu(menu, top_index)
            for item_index in range(self._user32.GetMenuItemCount(submenu)):
                item_text = self._normalize_menu_text(
                    self._menu_text(submenu, item_index)
                )
                if item_text in normalized_items:
                    command = int(self._user32.GetMenuItemID(submenu, item_index))
                    if command in (0, 0xFFFFFFFF):
                        break
                    return command
        raise LabSolutionsRuntimeError(
            f"LabSolutions menu item was not found: {top_aliases} -> {item_aliases}"
        )

    def _post(self, handle: int, message: int, wparam: int = 0) -> None:
        if not self._user32.PostMessageW(handle, message, wparam, 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def _send(
        self,
        handle: int,
        message: int,
        wparam: int = 0,
        lparam: int = 0,
    ) -> int:
        result = self._wintypes.LPARAM()
        sent = self._user32.SendMessageTimeoutW(
            handle,
            message,
            wparam,
            lparam,
            self._SMTO_ABORTIFHUNG,
            int(self.runtime.ui_message_timeout_seconds * 1000),
            ctypes.byref(result),
        )
        if not sent:
            raise LabSolutionsRuntimeError(
                f"LabSolutions window did not process message 0x{message:04X}"
            )
        return int(result.value)

    def _wait_until(
        self,
        predicate: Callable[[], object],
        description: str,
        *,
        timeout_seconds: float | None = None,
    ) -> object:
        deadline = time.monotonic() + (
            self.runtime.ui_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        raise LabSolutionsRuntimeError(
            f"Timed out waiting for LabSolutions {description}"
        )

    def _dialog(
        self, process_id: int, aliases: tuple[str, ...], *, enabled: bool = True
    ) -> int | None:
        normalized = {alias.casefold() for alias in aliases}
        matches = []
        for handle in self._windows(process_id=process_id):
            if not self._user32.IsWindowVisible(handle):
                continue
            if enabled and not self._user32.IsWindowEnabled(handle):
                continue
            if self._window_text(handle).casefold() in normalized:
                matches.append(handle)
        if len(matches) > 1:
            raise LabSolutionsRuntimeError(
                f"Multiple enabled LabSolutions dialogs matched {aliases}: {matches}"
            )
        return matches[0] if matches else None

    def _control(self, parent: int, control_id: int, class_name: str) -> int | None:
        matches = [
            handle
            for handle in self._children(parent)
            if self._user32.GetDlgCtrlID(handle) == control_id
            and self._class_name(handle) == class_name
        ]
        return matches[0] if len(matches) == 1 else None

    def _baseline_confirmation_dialog(self, process_id: int) -> int | None:
        matches: list[int] = []
        for handle in self._windows(process_id=process_id):
            if not self._user32.IsWindowVisible(handle):
                continue
            if not self._user32.IsWindowEnabled(handle):
                continue
            if self._class_name(handle) != "#32770":
                continue
            if self._control(handle, 6, "Button") is None:
                continue
            if self._control(handle, 7, "Button") is None:
                continue
            texts = [self._window_text(child) for child in self._children(handle)]
            if any(_is_baseline_confirmation_text(text) for text in texts):
                matches.append(handle)
        if len(matches) > 1:
            raise LabSolutionsRuntimeError(
                "Multiple parameter-change baseline prompts are open: "
                f"{matches}"
            )
        return matches[0] if matches else None

    def dismiss_parameter_change_baseline_prompt(
        self,
        window: SpectrumWindow,
        *,
        wait_seconds: float,
    ) -> bool:
        """Select No on the method-change prompt without starting a correction."""

        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            dialog = self._baseline_confirmation_dialog(window.process_id)
            if dialog is not None:
                self._post(dialog, self._WM_COMMAND, 7)
                self._wait_until(
                    lambda: not self._user32.IsWindow(dialog)
                    or not self._user32.IsWindowVisible(dialog),
                    "parameter-change baseline prompt to close",
                )
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(self.settings.poll_interval_seconds, 0.1))

    def _control_text(self, handle: int) -> str:
        buffer = ctypes.create_unicode_buffer(32768)
        self._send(
            handle,
            self._WM_GETTEXT,
            len(buffer),
            ctypes.cast(buffer, ctypes.c_void_p).value or 0,
        )
        return buffer.value

    def _set_control_text(self, handle: int, value: str) -> None:
        buffer = ctypes.create_unicode_buffer(value)
        result = self._send(
            handle,
            self._WM_SETTEXT,
            0,
            ctypes.cast(buffer, ctypes.c_void_p).value or 0,
        )
        if result == 0:
            raise LabSolutionsRuntimeError(
                "LabSolutions rejected the automatic-control directory value"
            )

    def _menu_window(self) -> SpectrumWindow | None:
        executable = self.runtime.executable
        mode = self.settings.mode
        markers = _MODE_TITLE_MARKERS.get(mode, ())
        for handle in self._windows():
            if not self._user32.IsWindowVisible(handle):
                continue
            process_id = self._wintypes.DWORD()
            self._user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
            process_path = self._process_path(process_id.value)
            if process_path is None or not _same_path(process_path, executable):
                continue
            title = self._window_text(handle)
            if markers and not any(
                marker.casefold() in title.casefold() for marker in markers
            ):
                continue
            try:
                self._find_menu_command(
                    handle,
                    ("\u4eea\u5668", "instrument"),
                    ("\u81ea\u52a8\u63a7\u5236", "automatic control"),
                )
            except LabSolutionsRuntimeError:
                continue
            return SpectrumWindow(
                process_id=process_id.value,
                handle=handle,
                title=title,
            )
        return None

    def ensure_spectrum_window(self) -> tuple[SpectrumWindow, bool]:
        existing = self._menu_window()
        if existing is not None:
            return existing, False
        executable = self.runtime.executable
        if not executable.is_file():
            raise LabSolutionsRuntimeError(
                f"LabSolutions executable does not exist: {executable}"
            )
        subprocess.Popen(
            [str(executable), *self.runtime.arguments],
            cwd=str(executable.parent),
        )
        window = self._wait_until(
            self._menu_window,
            f"{self.settings.mode} main window",
            timeout_seconds=self.runtime.startup_timeout_seconds,
        )
        assert isinstance(window, SpectrumWindow)
        return window, True

    def waiting_status(self, window: SpectrumWindow) -> str | None:
        for top in self._automatic_control_windows(window):
            status = self._control(top, 9012, "Static")
            assert status is not None
            text = self._window_text(status).strip()
            folded = text.casefold()
            if "\u5f85\u673a" in text or "waiting" in folded:
                return text
        return None

    def _automatic_control_windows(self, window: SpectrumWindow) -> list[int]:
        return [
            top
            for top in self._windows(process_id=window.process_id)
            if self._user32.IsWindowVisible(top)
            and self._control(top, 9012, "Static") is not None
        ]

    def leave_automatic_control(self, window: SpectrumWindow) -> None:
        if self.waiting_status(window) is None:
            return
        candidates: list[int] = []
        for top in self._windows(process_id=window.process_id):
            if self._control(top, 9012, "Static") is None:
                continue
            button = self._control(top, 2, "Button")
            if button is not None and self._user32.IsWindowEnabled(button):
                candidates.append(top)
        for dialog in candidates:
            self._post(dialog, self._WM_COMMAND, 2)
        self._wait_until(
            lambda: not self._automatic_control_windows(window),
            "automatic-control window to close",
        )

    def _open_customize(self, window: SpectrumWindow) -> int:
        if (
            self._dialog(window.process_id, ("\u81ea\u5b9a\u4e49", "customize"))
            is not None
        ):
            raise LabSolutionsRuntimeError("A Customize dialog is already open")
        command = self._find_menu_command(
            window.handle,
            ("\u5de5\u5177", "tools"),
            ("\u81ea\u5b9a\u4e49", "customize"),
        )
        self._post(window.handle, self._WM_COMMAND, command)
        dialog = self._wait_until(
            lambda: self._dialog(
                window.process_id, ("\u81ea\u5b9a\u4e49", "customize")
            ),
            "Customize dialog",
        )
        assert isinstance(dialog, int)
        return dialog

    def _automatic_control_edit(self, dialog: int) -> int:
        tab = self._control(dialog, 12320, "SysTabControl32")
        if tab is None:
            raise LabSolutionsRuntimeError("Customize tab control was not found")
        count = self._send(tab, self._TCM_GETITEMCOUNT)
        for _ in range(max(1, count)):
            edit = self._control(dialog, 9032, "Edit")
            if edit is not None and self._user32.IsWindowVisible(edit):
                return edit
            self._send(tab, self._WM_SETFOCUS)
            self._send(tab, self._WM_KEYDOWN, self._VK_RIGHT)
            self._send(tab, self._WM_KEYUP, self._VK_RIGHT)
            time.sleep(0.1)
        raise LabSolutionsRuntimeError(
            "Customize Automatic Control page or directory edit was not found"
        )

    def _close_dialog(self, dialog: int, button_id: int) -> None:
        button = self._control(dialog, button_id, "Button")
        if button is None:
            raise LabSolutionsRuntimeError(
                f"Customize dialog button {button_id} was not found"
            )
        self._post(button, self._BM_CLICK)
        self._wait_until(
            lambda: not self._user32.IsWindow(dialog), "Customize dialog to close"
        )

    def _read_configured_command_directory(self, window: SpectrumWindow) -> Path:
        dialog = self._open_customize(window)
        try:
            edit = self._automatic_control_edit(dialog)
            value = self._control_text(edit).strip()
            if not value:
                raise LabSolutionsRuntimeError(
                    "LabSolutions automatic-control directory is empty"
                )
            return Path(value)
        finally:
            if self._user32.IsWindow(dialog):
                self._close_dialog(dialog, 2)

    def ensure_command_directory(
        self,
        window: SpectrumWindow,
        expected: Path,
        *,
        allow_change: bool,
    ) -> bool:
        if not expected.is_dir():
            raise LabSolutionsRuntimeError(
                f"Configured command directory does not exist: {expected}"
            )
        dialog = self._open_customize(window)
        changed = False
        try:
            edit = self._automatic_control_edit(dialog)
            current = Path(self._control_text(edit).strip())
            if _same_path(current, expected):
                self._close_dialog(dialog, 2)
                return False
            if not allow_change:
                raise LabSolutionsRuntimeError(
                    "LabSolutions command directory does not match the MCP config: "
                    f"LabSolutions={current}, MCP={expected}"
                )
            self._set_control_text(edit, str(expected))
            if not _same_path(Path(self._control_text(edit).strip()), expected):
                raise LabSolutionsRuntimeError(
                    "LabSolutions command directory did not accept the configured path"
                )
            self._close_dialog(dialog, 1)
            changed = True
        finally:
            if self._user32.IsWindow(dialog):
                self._close_dialog(dialog, 2)

        persisted = self._read_configured_command_directory(window)
        if not _same_path(persisted, expected):
            raise LabSolutionsRuntimeError(
                "LabSolutions command directory was not persisted: "
                f"LabSolutions={persisted}, MCP={expected}"
            )
        return changed

    def enter_automatic_control(self, window: SpectrumWindow) -> str:
        current = self.waiting_status(window)
        if current is not None:
            return current
        instrument_panels = []
        for handle in self._windows(process_id=window.process_id):
            if not self._user32.IsWindowVisible(handle):
                continue
            spectrum_panel = (
                self._control(handle, 11002, "Static") is not None
                and self._control(handle, 1638, "Button") is not None
            )
            photometric_panel = (
                self._control(handle, 18002, "Static") is not None
                and self._control(handle, 1095, "Button") is not None
            )
            close = self._control(handle, 2, "Button")
            if (spectrum_panel or photometric_panel) and close is not None:
                instrument_panels.append((handle, close))
        for panel, close in instrument_panels:
            self._post(close, self._BM_CLICK)
            self._wait_until(
                lambda panel=panel: not self._user32.IsWindowVisible(panel),
                "Instrument Control panel to close before Automatic Control",
            )
        command = self._find_menu_command(
            window.handle,
            ("\u4eea\u5668", "instrument"),
            ("\u81ea\u52a8\u63a7\u5236", "automatic control"),
        )
        self._post(window.handle, self._WM_COMMAND, command)
        status = self._wait_until(
            lambda: self.waiting_status(window), "Automatic Control Waiting state"
        )
        assert isinstance(status, str)
        return status


class LabSolutionsRuntimeManager:
    """Guard physical workflows with a verified mode-specific runtime state."""

    def __init__(
        self,
        settings: ControlSettings,
        *,
        backend: RuntimeUiBackend | None = None,
        client_factory: Callable[[], LabSolutionsClient] | None = None,
    ) -> None:
        self.settings = settings
        self.backend = backend or WindowsLabSolutionsUi(settings)
        self._client_factory = client_factory or self._default_client
        self.lock_path = settings.command_dir / ".shimadzu_uvvis_runtime.lock"

    def _default_client(self) -> LabSolutionsClient:
        return LabSolutionsClient(
            command_dir=self.settings.command_dir,
            mode=self.settings.mode,
            timeout=self.settings.runtime.hello_timeout_seconds,
            poll_interval=self.settings.poll_interval_seconds,
            lock_timeout=self.settings.lock_timeout_seconds,
            encoding=self.settings.encoding,
            audit_dir=self.settings.audit_dir,
        )

    def _archive_failed_hello(self, client: LabSolutionsClient) -> Path | None:
        if not client.recovery_path.exists():
            return None
        try:
            marker = json.loads(client.recovery_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LabSolutionsRuntimeError(
                f"Cannot inspect runtime recovery marker: {client.recovery_path}"
            ) from exc
        if not isinstance(marker, dict) or marker.get("command") != 0:
            raise LabSolutionsRuntimeError(
                "Runtime readiness found a non-Hello recovery marker; automatic "
                f"cleanup is forbidden: {client.recovery_path}"
            )
        if client.command_path.exists():
            fields = parse_exchange_text(
                client.command_path.read_text(encoding="utf-8-sig")
            )
            if fields != {"Command": "0"}:
                raise LabSolutionsRuntimeError(
                    "Runtime readiness found a non-Hello pending command; automatic "
                    f"cleanup is forbidden: {client.command_path}"
                )
        if client.feedback_path.exists():
            fields = parse_exchange_text(
                client.feedback_path.read_text(encoding="utf-8-sig")
            )
            if fields.get("Command") != "0":
                raise LabSolutionsRuntimeError(
                    "Runtime readiness found feedback for a non-Hello command; "
                    f"automatic cleanup is forbidden: {client.feedback_path}"
                )

        archive_root = (
            self.settings.audit_dir or self.settings.command_dir
        ) / "runtime-recovery"
        archive = archive_root / (
            time.strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
        )
        archive.mkdir(parents=True, exist_ok=False)
        for source in (
            client.command_path,
            client.feedback_path,
            client.recovery_path,
        ):
            if source.exists():
                shutil.move(str(source), str(archive / source.name))
        return archive

    def _hello(self) -> Feedback:
        client = self._client_factory()
        try:
            return client.send_command(
                0, timeout=self.settings.runtime.hello_timeout_seconds
            )
        except Exception:
            self._archive_failed_hello(client)
            raise

    def _wait_for_waiting(self, window: SpectrumWindow) -> str:
        deadline = time.monotonic() + self.settings.runtime.ui_timeout_seconds
        while time.monotonic() < deadline:
            status = self.backend.waiting_status(window)
            if status is not None:
                return status
            time.sleep(min(self.settings.poll_interval_seconds, 0.1))
        raise LabSolutionsRuntimeError(
            "LabSolutions completed Hello but did not return to Automatic Control "
            "Waiting"
        )

    def dismiss_parameter_change_baseline_prompt(
        self, *, wait_seconds: float = 0.0
    ) -> bool:
        """Dismiss only LabSolutions' method-change correction prompt."""

        if not self.settings.runtime.enabled:
            raise LabSolutionsRuntimeError(
                "LabSolutions runtime management is disabled; set "
                "[runtime].enabled=true before executing MCP instrument workflows"
            )
        try:
            with InterProcessFileLock(
                self.lock_path,
                timeout=self.settings.lock_timeout_seconds,
                poll_interval=min(self.settings.poll_interval_seconds, 0.1),
            ):
                window, _launched = self.backend.ensure_spectrum_window()
                return self.backend.dismiss_parameter_change_baseline_prompt(
                    window,
                    wait_seconds=wait_seconds,
                )
        except FileLockTimeoutError as exc:
            raise LabSolutionsRuntimeError(
                "Another process is changing the LabSolutions runtime state"
            ) from exc

    def ensure_ready(self, *, allow_reconfigure: bool) -> RuntimeReady:
        """Return READY only after UI state, directory, and Hello are verified."""

        if not self.settings.runtime.enabled:
            raise LabSolutionsRuntimeError(
                "LabSolutions runtime management is disabled; set "
                "[runtime].enabled=true before executing MCP instrument workflows"
            )
        if self.settings.mode not in _MODE_APPLICATIONS:
            raise LabSolutionsRuntimeError(
                f"Unsupported LabSolutions runtime mode: {self.settings.mode!r}"
            )
        try:
            with InterProcessFileLock(
                self.lock_path,
                timeout=self.settings.lock_timeout_seconds,
                poll_interval=min(self.settings.poll_interval_seconds, 0.1),
            ):
                window, launched = self.backend.ensure_spectrum_window()
                self.backend.dismiss_parameter_change_baseline_prompt(
                    window,
                    wait_seconds=0.0,
                )
                status = self.backend.waiting_status(window)
                changed = False

                if status is not None:
                    try:
                        feedback = self._hello()
                    except Exception as first_error:
                        if not (
                            allow_reconfigure
                            and self.settings.runtime.configure_command_directory
                        ):
                            raise LabSolutionsRuntimeError(
                                "LabSolutions shows Waiting but the Hello handshake "
                                f"failed: {first_error}"
                            ) from first_error
                        self.backend.leave_automatic_control(window)
                    else:
                        status = self._wait_for_waiting(window)
                        return RuntimeReady(
                            process_id=window.process_id,
                            window_handle=window.handle,
                            launched=launched,
                            command_directory=self.settings.command_dir,
                            command_directory_changed=False,
                            waiting_status=status,
                            feedback=feedback,
                        )

                changed = self.backend.ensure_command_directory(
                    window,
                    self.settings.command_dir,
                    allow_change=(
                        allow_reconfigure
                        and self.settings.runtime.configure_command_directory
                    ),
                )
                status = self.backend.enter_automatic_control(window)
                try:
                    feedback = self._hello()
                except Exception as exc:
                    raise LabSolutionsRuntimeError(
                        "LabSolutions entered Waiting but did not complete the "
                        f"Command=0 handshake in {self.settings.command_dir}: {exc}"
                    ) from exc
                status = self._wait_for_waiting(window)
                return RuntimeReady(
                    process_id=window.process_id,
                    window_handle=window.handle,
                    launched=launched,
                    command_directory=self.settings.command_dir,
                    command_directory_changed=changed,
                    waiting_status=status,
                    feedback=feedback,
                )
        except FileLockTimeoutError as exc:
            raise LabSolutionsRuntimeError(
                "Another process is changing the LabSolutions runtime state"
            ) from exc
