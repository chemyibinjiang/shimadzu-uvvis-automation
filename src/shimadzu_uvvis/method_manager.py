"""Generate and verify LabSolutions Spectrum method files through Win32 controls."""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .audit import write_json_atomic
from .configuration import ControlSettings, MethodTemplate
from .locking import FileLockTimeoutError, InterProcessFileLock
from .measurements import (
    PHOTOMETRIC_METHOD_WAVELENGTH_LIMIT,
    SPECTRUM_DATA_INTERVALS_NM,
    MeasurementPlanError,
    MeasurementRequest,
    method_generation_requests,
    resolve_method_template,
)
from .runtime_manager import (
    LabSolutionsRuntimeError,
    LabSolutionsRuntimeManager,
    RuntimeReady,
    SpectrumWindow,
    WindowsLabSolutionsUi,
    _is_baseline_confirmation_text,
    settings_for_mode,
)


MAX_VERIFIED_PHOTOMETRIC_WAVELENGTHS = PHOTOMETRIC_METHOD_WAVELENGTH_LIMIT
_INTERVAL_LABELS = {
    0.01: "0.01",
    0.05: "0.05",
    0.1: "0.1",
    0.2: "0.2",
    0.5: "0.5",
    1.0: "1.0",
    2.0: "2.0",
    5.0: "5.0",
}


class SpectrumMethodGenerationError(RuntimeError):
    """Raised when a requested Spectrum method cannot be safely generated."""


def spectrum_generation_support(request: MeasurementRequest) -> tuple[bool, str]:
    """Report whether the installed Spectrum editor can represent this request."""

    if request.mode != "spectrum":
        return False, "Automatic method generation currently supports Spectrum only."
    if request.signal_type != "absorbance":
        return False, "Only the verified Spectrum absorbance template is supported."
    step_nm = float(request.parameters["step_nm"])
    if not any(
        math.isclose(step_nm, value, abs_tol=1e-9)
        for value in SPECTRUM_DATA_INTERVALS_NM
    ):
        supported = ", ".join(f"{value:g}" for value in SPECTRUM_DATA_INTERVALS_NM)
        return (
            False,
            "The installed LabSolutions Spectrum editor only offers these data "
            f"intervals (nm): {supported}. Use Photometric for an exact discrete "
            "wavelength list such as 400, 410, ..., 700 nm.",
        )
    return True, "The request is representable by the verified Spectrum editor."


def photometric_generation_support(
    request: MeasurementRequest,
) -> tuple[bool, str]:
    """Report whether the verified Photometric editor path supports the request."""

    if request.mode != "photometric":
        return False, "This generator requires Photometric mode."
    if request.signal_type != "absorbance":
        return False, "Only the verified Photometric absorbance template is supported."
    wavelengths = request.parameters["wavelengths_nm"]
    if not isinstance(wavelengths, list) or not wavelengths:
        return False, "Photometric generation requires at least one wavelength."
    segment_count = math.ceil(len(wavelengths) / MAX_VERIFIED_PHOTOMETRIC_WAVELENGTHS)
    if segment_count > 1:
        return (
            True,
            "The installed Photometric editor accepts at most "
            f"{MAX_VERIFIED_PHOTOMETRIC_WAVELENGTHS} registered wavelengths per "
            f"method. The request will be split into {segment_count} verified methods.",
        )
    return True, "The request is representable by one verified Photometric method."


def method_generation_support(request: MeasurementRequest) -> tuple[bool, str]:
    if request.mode == "spectrum":
        return spectrum_generation_support(request)
    if request.mode == "photometric":
        return photometric_generation_support(request)
    return False, f"Automatic method generation does not yet support {request.mode}."


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


@dataclass(frozen=True, slots=True)
class SpectrumMethodReadback:
    start_nm: float
    end_nm: float
    data_interval_nm: float
    signal_type: str
    signal_label: str

    def as_dict(self) -> dict[str, object]:
        return {
            "labsolutions_start_nm": self.start_nm,
            "labsolutions_end_nm": self.end_nm,
            "data_interval_nm": self.data_interval_nm,
            "signal_type": self.signal_type,
            "signal_label": self.signal_label,
        }


class SpectrumMethodUiBackend(Protocol):
    def generate_and_verify(
        self,
        *,
        template_file: Path,
        target_file: Path,
        upper_nm: float,
        lower_nm: float,
        step_nm: float,
        signal_type: str,
    ) -> SpectrumMethodReadback: ...


class WindowsSpectrumMethodUi(WindowsLabSolutionsUi):
    """LabSolutions 1.13 Spectrum method editor automation by stable control IDs."""

    _SW_RESTORE = 9
    _CB_GETCOUNT = 0x0146
    _CB_GETCURSEL = 0x0147
    _CB_GETLBTEXT = 0x0148
    _CB_GETLBTEXTLEN = 0x0149
    _CB_SETCURSEL = 0x014E
    _CBN_SELCHANGE = 1

    def _configure_signatures(self) -> None:
        super()._configure_signatures()
        w = self._wintypes
        u = self._user32
        u.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
        u.ShowWindow.restype = w.BOOL
        u.IsIconic.argtypes = [w.HWND]
        u.IsIconic.restype = w.BOOL
        u.IsWindowEnabled.argtypes = [w.HWND]
        u.IsWindowEnabled.restype = w.BOOL
        u.GetParent.argtypes = [w.HWND]
        u.GetParent.restype = w.HWND

    def _restore(self, window: SpectrumWindow) -> None:
        if self._user32.IsIconic(window.handle):
            self._user32.ShowWindow(window.handle, self._SW_RESTORE)
            self._wait_until(
                lambda: not self._user32.IsIconic(window.handle),
                "Spectrum main window to restore",
            )

    def _top_dialog_matching(
        self,
        process_id: int,
        predicate: Callable[[str], bool],
        *,
        enabled: bool = True,
    ) -> int | None:
        matches: list[int] = []
        for handle in self._windows(process_id=process_id):
            if not self._user32.IsWindowVisible(handle):
                continue
            if enabled and not self._user32.IsWindowEnabled(handle):
                continue
            if predicate(self._window_text(handle).strip()):
                matches.append(handle)
        if len(matches) > 1:
            raise SpectrumMethodGenerationError(
                f"Multiple LabSolutions dialogs matched during method generation: {matches}"
            )
        return matches[0] if matches else None

    def _instrument_panel(self, process_id: int) -> int | None:
        return self._top_dialog_matching(
            process_id,
            lambda title: title.casefold()
            in {"仪器控制面板", "instrument control panel"},
            enabled=False,
        )

    def _initialization_dialog(self, process_id: int) -> int | None:
        return self._top_dialog_matching(
            process_id,
            lambda title: title.startswith("UV-2700") or title.startswith("UV-2700i"),
        )

    def _parameter_dialog(self, process_id: int) -> int | None:
        return self._top_dialog_matching(
            process_id,
            lambda title: title.startswith("参数 - ")
            or title.startswith("Parameter - "),
        )

    def _file_dialog(self, process_id: int) -> int | None:
        matches: list[int] = []
        for handle in self._windows(process_id=process_id):
            if not self._user32.IsWindowVisible(handle):
                continue
            if not self._user32.IsWindowEnabled(handle):
                continue
            if self._class_name(handle) != "#32770":
                continue
            if self._file_name_edit(handle) is None:
                continue
            if self._control(handle, 1, "Button") is None:
                continue
            matches.append(handle)
        if len(matches) > 1:
            raise SpectrumMethodGenerationError(
                f"Multiple LabSolutions file dialogs are open: {matches}"
            )
        return matches[0] if matches else None

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
            raise SpectrumMethodGenerationError(
                f"Multiple baseline-confirmation dialogs are open: {matches}"
            )
        return matches[0] if matches else None

    def _decline_baseline_confirmation(self, process_id: int) -> None:
        window = SpectrumWindow(process_id=process_id, handle=0, title="")
        self.dismiss_parameter_change_baseline_prompt(
            window,
            wait_seconds=min(2.0, self.runtime.ui_timeout_seconds),
        )

    def _file_name_edit(self, dialog: int) -> int | None:
        for control_id in (1148, 1001):
            edit = self._control(dialog, control_id, "Edit")
            if edit is not None:
                return edit
        return None

    def _required_control(self, parent: int, control_id: int, class_name: str) -> int:
        control = self._control(parent, control_id, class_name)
        if control is None:
            raise SpectrumMethodGenerationError(
                f"LabSolutions control {control_id}/{class_name} was not found"
            )
        return control

    def _click(self, parent: int, control_id: int) -> None:
        button = self._required_control(parent, control_id, "Button")
        if not self._user32.IsWindowEnabled(button):
            raise SpectrumMethodGenerationError(
                f"LabSolutions button {control_id} is disabled"
            )
        self._post(button, self._BM_CLICK)

    def _wait_closed(self, handle: int, description: str) -> None:
        self._wait_until(
            lambda: not self._user32.IsWindow(handle),
            description,
        )

    def _panel_connected(self, panel: int) -> bool:
        if not self._user32.IsWindowEnabled(panel):
            return False
        wavelength = self._control(panel, 11002, "Static")
        edit = self._control(panel, 1638, "Button")
        if wavelength is None or edit is None:
            return False
        value = self._window_text(wavelength).strip()
        return bool(
            value and not value.startswith("-") and self._user32.IsWindowEnabled(edit)
        )

    def _connect_instrument(self, window: SpectrumWindow) -> int:
        panel = self._instrument_panel(window.process_id)
        if panel is not None and self._panel_connected(panel):
            return panel

        command = self._find_menu_command(
            window.handle,
            ("仪器", "instrument"),
            ("连接", "connect"),
        )
        self._post(window.handle, self._WM_COMMAND, command)
        panel_value = self._wait_until(
            lambda: self._instrument_panel(window.process_id),
            "Instrument Control panel",
            timeout_seconds=self.runtime.startup_timeout_seconds,
        )
        assert isinstance(panel_value, int)
        panel = panel_value

        initialization = self._wait_until(
            lambda: self._initialization_dialog(window.process_id)
            or (panel if self._panel_connected(panel) else None),
            "UV-2700 initialization",
            timeout_seconds=self.runtime.startup_timeout_seconds,
        )
        assert isinstance(initialization, int)
        if initialization != panel:
            ok = self._required_control(initialization, 1, "Button")
            self._wait_until(
                lambda: self._user32.IsWindowEnabled(ok),
                "UV-2700 initialization to complete",
                timeout_seconds=self.runtime.startup_timeout_seconds,
            )
            self._post(ok, self._BM_CLICK)
            self._wait_closed(initialization, "UV-2700 initialization dialog to close")

        self._wait_until(
            lambda: self._panel_connected(panel),
            "connected Instrument Control panel",
            timeout_seconds=self.runtime.startup_timeout_seconds,
        )
        return panel

    def _choose_file(self, process_id: int, path: Path) -> None:
        dialog_value = self._wait_until(
            lambda: self._file_dialog(process_id),
            "method file dialog",
        )
        assert isinstance(dialog_value, int)
        dialog = dialog_value
        filename = self._file_name_edit(dialog)
        if filename is None:
            raise SpectrumMethodGenerationError(
                "LabSolutions file dialog filename field was not found"
            )
        self._set_control_text(filename, str(path))
        self._click(dialog, 1)
        self._wait_closed(dialog, "method file dialog to close")

    def _load_method(self, panel: int, process_id: int, path: Path) -> int:
        self._click(panel, 1635)
        self._choose_file(process_id, path)
        self._click(panel, 1638)
        dialog_value = self._wait_until(
            lambda: self._parameter_dialog(process_id),
            "Spectrum parameter editor",
        )
        assert isinstance(dialog_value, int)
        return dialog_value

    def _combo_items(self, combo: int) -> list[str]:
        count = self._send(combo, self._CB_GETCOUNT)
        if count < 0:
            raise SpectrumMethodGenerationError("Cannot read LabSolutions combo box")
        items: list[str] = []
        for index in range(count):
            length = self._send(combo, self._CB_GETLBTEXTLEN, index)
            buffer = ctypes.create_unicode_buffer(max(2, length + 1))
            self._send(
                combo,
                self._CB_GETLBTEXT,
                index,
                ctypes.cast(buffer, ctypes.c_void_p).value or 0,
            )
            items.append(buffer.value)
        return items

    def _select_combo(self, combo: int, label: str) -> None:
        items = self._combo_items(combo)
        try:
            index = items.index(label)
        except ValueError as exc:
            raise SpectrumMethodGenerationError(
                f"LabSolutions combo does not contain {label!r}; available: {items}"
            ) from exc
        if self._send(combo, self._CB_SETCURSEL, index) != index:
            raise SpectrumMethodGenerationError(
                f"LabSolutions did not select combo value {label!r}"
            )
        parent = self._user32.GetParent(combo)
        control_id = self._user32.GetDlgCtrlID(combo)
        notification = (self._CBN_SELCHANGE << 16) | (control_id & 0xFFFF)
        self._send(parent, self._WM_COMMAND, notification, combo)

    def _read_parameters(self, dialog: int) -> SpectrumMethodReadback:
        start = self._required_control(dialog, 1005, "Edit")
        end = self._required_control(dialog, 1157, "Edit")
        interval = self._required_control(dialog, 21679, "ComboBox")
        signal = self._required_control(dialog, 21681, "ComboBox")
        signal_index = self._send(signal, self._CB_GETCURSEL)
        signal_types = {
            0: "absorbance",
            1: "transmittance",
            2: "reflectance",
            3: "energy",
        }
        if signal_index not in signal_types:
            raise SpectrumMethodGenerationError(
                f"Unsupported LabSolutions signal selection index: {signal_index}"
            )
        try:
            return SpectrumMethodReadback(
                start_nm=float(self._control_text(start).strip()),
                end_nm=float(self._control_text(end).strip()),
                data_interval_nm=float(self._control_text(interval).strip()),
                signal_type=signal_types[signal_index],
                signal_label=self._control_text(signal).strip(),
            )
        except ValueError as exc:
            raise SpectrumMethodGenerationError(
                "LabSolutions returned a non-numeric Spectrum parameter"
            ) from exc

    def _set_parameters(
        self,
        dialog: int,
        *,
        upper_nm: float,
        lower_nm: float,
        step_nm: float,
        signal_type: str,
    ) -> SpectrumMethodReadback:
        if signal_type != "absorbance":
            raise SpectrumMethodGenerationError(
                "Only absorbance generation is verified"
            )
        start = self._required_control(dialog, 1005, "Edit")
        end = self._required_control(dialog, 1157, "Edit")
        interval = self._required_control(dialog, 21679, "ComboBox")
        signal = self._required_control(dialog, 21681, "ComboBox")
        self._set_control_text(start, f"{upper_nm:g}")
        self._set_control_text(end, f"{lower_nm:g}")
        label = next(
            value
            for key, value in _INTERVAL_LABELS.items()
            if math.isclose(step_nm, key, abs_tol=1e-9)
        )
        self._select_combo(interval, label)
        items = self._combo_items(signal)
        if not items:
            raise SpectrumMethodGenerationError("LabSolutions signal list is empty")
        self._select_combo(signal, items[0])
        return self._read_parameters(dialog)

    @staticmethod
    def _matches(
        readback: SpectrumMethodReadback,
        *,
        upper_nm: float,
        lower_nm: float,
        step_nm: float,
        signal_type: str,
    ) -> bool:
        return (
            math.isclose(readback.start_nm, upper_nm, abs_tol=1e-9)
            and math.isclose(readback.end_nm, lower_nm, abs_tol=1e-9)
            and math.isclose(readback.data_interval_nm, step_nm, abs_tol=1e-9)
            and readback.signal_type == signal_type
        )

    def _save_as(self, dialog: int, process_id: int, target_file: Path) -> None:
        self._click(dialog, 1163)
        self._choose_file(process_id, target_file)
        self._wait_closed(dialog, "Spectrum parameter editor to close after Save As")
        self._wait_until(target_file.is_file, f"generated method file {target_file}")
        self._decline_baseline_confirmation(process_id)

    def _cleanup(self, window: SpectrumWindow) -> None:
        try:
            self._decline_baseline_confirmation(window.process_id)
        except Exception:
            pass
        file_dialog = self._file_dialog(window.process_id)
        if file_dialog is not None:
            try:
                self._click(file_dialog, 2)
                self._wait_closed(file_dialog, "method file dialog to cancel")
            except Exception:
                pass
        parameter = self._parameter_dialog(window.process_id)
        if parameter is not None:
            try:
                self._click(parameter, 2)
                self._wait_closed(parameter, "Spectrum parameter editor to cancel")
            except Exception:
                pass
        panel = self._instrument_panel(window.process_id)
        if panel is not None and self._user32.IsWindowVisible(panel):
            try:
                self._click(panel, 2)
                self._wait_until(
                    lambda: not self._user32.IsWindowVisible(panel),
                    "Instrument Control panel to close",
                )
            except Exception:
                pass
        try:
            command = self._find_menu_command(
                window.handle,
                ("仪器", "instrument"),
                ("断开", "disconnect"),
            )
            self._post(window.handle, self._WM_COMMAND, command)
            time.sleep(0.5)
        except Exception:
            pass

    def generate_and_verify(
        self,
        *,
        template_file: Path,
        target_file: Path,
        upper_nm: float,
        lower_nm: float,
        step_nm: float,
        signal_type: str,
    ) -> SpectrumMethodReadback:
        window, _launched = self.ensure_spectrum_window()
        self._restore(window)
        panel: int | None = None
        try:
            self.leave_automatic_control(window)
            self._wait_until(
                lambda: self._user32.IsWindowEnabled(window.handle),
                "Spectrum main window to enable after Automatic Control",
                timeout_seconds=self.runtime.startup_timeout_seconds,
            )
            panel = self._connect_instrument(window)
            parameter = self._load_method(panel, window.process_id, template_file)
            requested = self._set_parameters(
                parameter,
                upper_nm=upper_nm,
                lower_nm=lower_nm,
                step_nm=step_nm,
                signal_type=signal_type,
            )
            if not self._matches(
                requested,
                upper_nm=upper_nm,
                lower_nm=lower_nm,
                step_nm=step_nm,
                signal_type=signal_type,
            ):
                raise SpectrumMethodGenerationError(
                    f"LabSolutions rejected requested parameters: {requested.as_dict()}"
                )
            self._save_as(parameter, window.process_id, target_file)

            verification = self._load_method(panel, window.process_id, target_file)
            readback = self._read_parameters(verification)
            self._click(verification, 2)
            self._wait_closed(
                verification, "verified Spectrum parameter editor to close"
            )
            if not self._matches(
                readback,
                upper_nm=upper_nm,
                lower_nm=lower_nm,
                step_nm=step_nm,
                signal_type=signal_type,
            ):
                raise SpectrumMethodGenerationError(
                    f"Generated method read-back mismatch: {readback.as_dict()}"
                )
            return readback
        finally:
            self._cleanup(window)


class SpectrumMethodManager:
    """Validate, generate, attest, and safely publish one Spectrum method."""

    def __init__(
        self,
        settings: ControlSettings,
        *,
        backend: SpectrumMethodUiBackend | None = None,
        runtime_manager_factory: Callable[[], LabSolutionsRuntimeManager] | None = None,
    ) -> None:
        self.settings = settings
        self.backend = backend or WindowsSpectrumMethodUi(settings)
        self._runtime_manager_factory = runtime_manager_factory or (
            lambda: LabSolutionsRuntimeManager(settings)
        )

    @property
    def _controller_lock_path(self) -> Path:
        if self.settings.data_dir is None:
            raise SpectrumMethodGenerationError(
                "spectrum.data_dir must be configured for method generation"
            )
        return self.settings.data_dir / ".spectrum_batch_controller.lock"

    @property
    def _active_batch_path(self) -> Path:
        if self.settings.data_dir is None:
            raise SpectrumMethodGenerationError(
                "spectrum.data_dir must be configured for method generation"
            )
        return self.settings.data_dir / ".active_spectrum_batch.json"

    @staticmethod
    def _manifest_path(target: Path) -> Path:
        return target.with_suffix(target.suffix + ".generation.json")

    def _assert_no_active_batch(self) -> None:
        if self._active_batch_path.exists():
            raise SpectrumMethodGenerationError(
                "Cannot edit a LabSolutions method while a Spectrum batch is active: "
                f"{self._active_batch_path}"
            )

    def _validate_template(self, template: MethodTemplate) -> str:
        if not template.method_file.is_file():
            raise SpectrumMethodGenerationError(
                f"Spectrum template does not exist: {template.method_file}"
            )
        digest = _sha256(template.method_file)
        if template.sha256 is not None and digest != template.sha256:
            raise SpectrumMethodGenerationError(
                "Spectrum template hash mismatch: "
                f"expected {template.sha256}, got {digest}"
            )
        return digest

    def _reuse_existing(
        self,
        *,
        target: Path,
        template_sha256: str,
        request: MeasurementRequest,
    ) -> dict[str, Any] | None:
        manifest_path = self._manifest_path(target)
        if not target.exists() and not manifest_path.exists():
            return None
        if not target.is_file() or not manifest_path.is_file():
            raise SpectrumMethodGenerationError(
                f"Generated method or its attestation is incomplete: {target}"
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SpectrumMethodGenerationError(
                f"Cannot read generated method attestation: {manifest_path}"
            ) from exc
        expected = {
            "method_file": str(target),
            "method_sha256": _sha256(target),
            "template_sha256": template_sha256,
            "request": {"signal_type": request.signal_type, **dict(request.parameters)},
        }
        mismatches = [
            key for key, value in expected.items() if manifest.get(key) != value
        ]
        if mismatches:
            raise SpectrumMethodGenerationError(
                "Existing generated method cannot be reused because its attestation "
                f"does not match: {', '.join(mismatches)}"
            )
        return manifest

    @staticmethod
    def _runtime_dict(ready: RuntimeReady) -> dict[str, object]:
        return ready.as_dict()

    def generate(
        self,
        request: MeasurementRequest,
        *,
        template_name: str | None = None,
    ) -> dict[str, Any]:
        supported, reason = spectrum_generation_support(request)
        if not supported:
            raise MeasurementPlanError(reason)
        resolved = resolve_method_template(
            self.settings.method_templates,
            self.settings.generated_method_dir,
            request,
            template_name=template_name,
        )
        template = resolved.template
        target = resolved.generated_method_file.resolve()
        generated_root = self.settings.generated_method_dir.resolve()
        if target.parent != generated_root or target.suffix.lower() != ".vspm":
            raise SpectrumMethodGenerationError(
                "Generated Spectrum method must be a .vspm directly under "
                f"{generated_root}"
            )
        generated_root.mkdir(parents=True, exist_ok=True)
        if self.settings.data_dir is not None:
            self.settings.data_dir.mkdir(parents=True, exist_ok=True)

        template_sha256 = self._validate_template(template)
        try:
            with InterProcessFileLock(
                self._controller_lock_path,
                timeout=self.settings.lock_timeout_seconds,
                poll_interval=min(self.settings.poll_interval_seconds, 0.1),
            ):
                self._assert_no_active_batch()
                existing = self._reuse_existing(
                    target=target,
                    template_sha256=template_sha256,
                    request=request,
                )
                if existing is not None:
                    return {**existing, "status": "reused", "created": False}

                runtime_before = self._runtime_manager_factory().ensure_ready(
                    allow_reconfigure=True
                )
                generation_error: Exception | None = None
                readback: SpectrumMethodReadback | None = None
                try:
                    readback = self.backend.generate_and_verify(
                        template_file=template.method_file,
                        target_file=target,
                        upper_nm=float(request.parameters["upper_nm"]),
                        lower_nm=float(request.parameters["lower_nm"]),
                        step_nm=float(request.parameters["step_nm"]),
                        signal_type=request.signal_type,
                    )
                except Exception as exc:
                    generation_error = exc
                try:
                    runtime_after = self._runtime_manager_factory().ensure_ready(
                        allow_reconfigure=False
                    )
                except Exception as restore_error:
                    if generation_error is not None:
                        raise SpectrumMethodGenerationError(
                            "Method generation failed and Automatic Control could not "
                            f"be restored. Generation error: {generation_error}; "
                            f"restore error: {restore_error}"
                        ) from restore_error
                    raise SpectrumMethodGenerationError(
                        "Generated method was saved but Automatic Control could not be "
                        f"restored: {restore_error}"
                    ) from restore_error
                if generation_error is not None:
                    raise SpectrumMethodGenerationError(
                        f"LabSolutions method generation failed: {generation_error}"
                    ) from generation_error
                assert readback is not None

                if not target.is_file():
                    raise SpectrumMethodGenerationError(
                        f"LabSolutions did not create the generated method: {target}"
                    )
                if _sha256(template.method_file) != template_sha256:
                    raise SpectrumMethodGenerationError(
                        "The immutable Spectrum template changed during generation"
                    )
                payload: dict[str, Any] = {
                    "schema_version": 1,
                    "status": "generated",
                    "created": True,
                    "mode": "spectrum",
                    "method_file": str(target),
                    "method_sha256": _sha256(target),
                    "template_file": str(template.method_file),
                    "template_sha256": template_sha256,
                    "request": {
                        "signal_type": request.signal_type,
                        **dict(request.parameters),
                    },
                    "readback": readback.as_dict(),
                    "runtime_before": self._runtime_dict(runtime_before),
                    "runtime_after": self._runtime_dict(runtime_after),
                }
                write_json_atomic(self._manifest_path(target), payload)
                return payload
        except FileLockTimeoutError as exc:
            raise SpectrumMethodGenerationError(
                "Another process is changing the Spectrum batch or method state"
            ) from exc
        except LabSolutionsRuntimeError:
            raise


class PhotometricMethodGenerationError(SpectrumMethodGenerationError):
    """Raised when a Photometric method cannot be generated and verified."""


@dataclass(frozen=True, slots=True)
class PhotometricMethodReadback:
    wavelengths_nm: tuple[float, ...]
    signal_type: str
    signal_label: str
    measurement_method_label: str

    def as_dict(self) -> dict[str, object]:
        return {
            "wavelengths_nm": list(self.wavelengths_nm),
            "wavelength_count": len(self.wavelengths_nm),
            "signal_type": self.signal_type,
            "signal_label": self.signal_label,
            "measurement_method_label": self.measurement_method_label,
        }


class PhotometricMethodUiBackend(Protocol):
    def generate_and_verify(
        self,
        *,
        template_file: Path,
        target_file: Path,
        wavelengths_nm: list[float],
        signal_type: str,
    ) -> PhotometricMethodReadback: ...


class WindowsPhotometricMethodUi(WindowsSpectrumMethodUi):
    """LabSolutions 1.13 Photometric editor automation by stable control IDs."""

    _SW_SHOW = 5
    _LVM_GETITEMCOUNT = 0x1004
    _LVM_SETITEMSTATE = 0x102B
    _LVM_GETITEMTEXTW = 0x1073
    _LVIF_TEXT = 0x0001
    _LVIF_STATE = 0x0008
    _LVIS_FOCUSED_AND_SELECTED = 0x0003

    def _instrument_panel(self, process_id: int) -> int | None:
        matches = [
            handle
            for handle in self._windows(process_id=process_id)
            if self._control(handle, 18002, "Static") is not None
            and self._control(handle, 5091, "Button") is not None
            and self._control(handle, 1095, "Button") is not None
        ]
        if len(matches) > 1:
            raise PhotometricMethodGenerationError(
                f"Multiple Photometric instrument panels matched: {matches}"
            )
        return matches[0] if matches else None

    def _parameter_dialog(self, process_id: int) -> int | None:
        matches = [
            handle
            for handle in self._windows(process_id=process_id)
            if self._user32.IsWindowVisible(handle)
            and self._control(handle, 1165, "Button") is not None
            and self._control(handle, 1163, "Button") is not None
            and self._control(handle, 2, "Button") is not None
        ]
        if len(matches) > 1:
            raise PhotometricMethodGenerationError(
                f"Multiple Photometric parameter dialogs matched: {matches}"
            )
        return matches[0] if matches else None

    def _wavelength_dialog(self, process_id: int) -> int | None:
        matches = [
            handle
            for handle in self._windows(process_id=process_id)
            if self._user32.IsWindowVisible(handle)
            and self._control(handle, 5169, "Button") is not None
            and self._control(handle, 5170, "Button") is not None
            and self._control(handle, 1182, "SysListView32") is not None
        ]
        if len(matches) > 1:
            raise PhotometricMethodGenerationError(
                f"Multiple registered-wavelength dialogs matched: {matches}"
            )
        return matches[0] if matches else None

    def _panel_connected(self, panel: int) -> bool:
        wavelength = self._control(panel, 18002, "Static")
        editor = self._control(panel, 1095, "Button")
        if wavelength is None or editor is None:
            return False
        value = self._window_text(wavelength).strip()
        return bool(
            value and not value.startswith("-") and self._user32.IsWindowEnabled(editor)
        )

    def _connect_instrument(self, window: SpectrumWindow) -> int:
        panel = self._instrument_panel(window.process_id)
        if panel is not None:
            self._user32.ShowWindow(panel, self._SW_SHOW)
            self._wait_until(
                lambda: self._user32.IsWindowVisible(panel),
                "Photometric Instrument Control panel to show",
            )
            if self._panel_connected(panel):
                return panel

        command = self._find_menu_command(
            window.handle,
            ("仪器", "instrument"),
            ("连接", "connect"),
        )
        self._post(window.handle, self._WM_COMMAND, command)
        panel_value = self._wait_until(
            lambda: self._instrument_panel(window.process_id),
            "Photometric Instrument Control panel",
            timeout_seconds=self.runtime.startup_timeout_seconds,
        )
        assert isinstance(panel_value, int)
        panel = panel_value
        self._user32.ShowWindow(panel, self._SW_SHOW)

        initialization = self._wait_until(
            lambda: self._initialization_dialog(window.process_id)
            or (panel if self._panel_connected(panel) else None),
            "UV-2700 initialization",
            timeout_seconds=self.runtime.startup_timeout_seconds,
        )
        assert isinstance(initialization, int)
        if initialization != panel:
            ok = self._required_control(initialization, 1, "Button")
            self._wait_until(
                lambda: self._user32.IsWindowEnabled(ok),
                "UV-2700 initialization to complete",
                timeout_seconds=self.runtime.startup_timeout_seconds,
            )
            self._post(ok, self._BM_CLICK)
            self._wait_closed(initialization, "UV-2700 initialization dialog to close")
        self._wait_until(
            lambda: self._panel_connected(panel),
            "connected Photometric Instrument Control panel",
            timeout_seconds=self.runtime.startup_timeout_seconds,
        )
        return panel

    def _load_method(self, panel: int, process_id: int, path: Path) -> int:
        self._click(panel, 5091)
        self._choose_file(process_id, path)
        self._click(panel, 1095)
        value = self._wait_until(
            lambda: self._parameter_dialog(process_id),
            "Photometric parameter editor",
        )
        assert isinstance(value, int)
        return value

    def _open_wavelengths(self, parameter: int, process_id: int) -> int:
        self._click(parameter, 1165)
        value = self._wait_until(
            lambda: self._wavelength_dialog(process_id),
            "registered-wavelength editor",
        )
        assert isinstance(value, int)
        return value

    @staticmethod
    def _lvitem32(
        *,
        mask: int,
        row: int,
        column: int,
        state: int = 0,
        state_mask: int = 0,
        text_pointer: int = 0,
        text_max: int = 0,
    ) -> bytes:
        # UVNavi.exe is 32-bit, so its remote LVITEM pointers are 32-bit.
        return struct.pack(
            "<IiiIIIiiiiiIIII",
            mask,
            row,
            column,
            state,
            state_mask,
            text_pointer,
            text_max,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        )

    def _with_remote_list_memory(
        self,
        process_id: int,
        operation: Callable[[int, int], Any],
    ) -> Any:
        w = self._wintypes
        k = self._kernel32
        k.VirtualAllocEx.argtypes = [
            w.HANDLE,
            ctypes.c_void_p,
            ctypes.c_size_t,
            w.DWORD,
            w.DWORD,
        ]
        k.VirtualAllocEx.restype = ctypes.c_void_p
        k.VirtualFreeEx.argtypes = [
            w.HANDLE,
            ctypes.c_void_p,
            ctypes.c_size_t,
            w.DWORD,
        ]
        k.VirtualFreeEx.restype = w.BOOL
        k.WriteProcessMemory.argtypes = [
            w.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        k.WriteProcessMemory.restype = w.BOOL
        k.ReadProcessMemory.argtypes = [
            w.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        k.ReadProcessMemory.restype = w.BOOL
        process = k.OpenProcess(0x0008 | 0x0010 | 0x0020 | 0x0400, False, process_id)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        remote = k.VirtualAllocEx(process, None, 4096, 0x1000 | 0x2000, 0x04)
        if not remote:
            k.CloseHandle(process)
            raise ctypes.WinError(ctypes.get_last_error())
        address = int(remote)
        if address > 0xFFFFFFFF:
            k.VirtualFreeEx(process, remote, 0, 0x8000)
            k.CloseHandle(process)
            raise PhotometricMethodGenerationError(
                "UVNavi remote memory is not addressable by a 32-bit LVITEM"
            )
        try:
            return operation(int(process), address)
        finally:
            k.VirtualFreeEx(process, remote, 0, 0x8000)
            k.CloseHandle(process)

    def _write_process(self, process: int, address: int, data: bytes) -> None:
        buffer = ctypes.create_string_buffer(data)
        written = ctypes.c_size_t()
        if not self._kernel32.WriteProcessMemory(
            process,
            ctypes.c_void_p(address),
            buffer,
            len(data),
            ctypes.byref(written),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def _read_process(self, process: int, address: int, size: int) -> bytes:
        buffer = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t()
        if not self._kernel32.ReadProcessMemory(
            process,
            ctypes.c_void_p(address),
            buffer,
            size,
            ctypes.byref(read),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.raw[: read.value]

    def _list_rows(self, list_view: int, process_id: int) -> list[list[str]]:
        count = self._send(list_view, self._LVM_GETITEMCOUNT)

        def operation(process: int, remote: int) -> list[list[str]]:
            rows: list[list[str]] = []
            text_address = remote + 256
            for row in range(count):
                values: list[str] = []
                for column in range(3):
                    self._write_process(process, text_address, b"\0" * 1024)
                    item = self._lvitem32(
                        mask=self._LVIF_TEXT,
                        row=row,
                        column=column,
                        text_pointer=text_address,
                        text_max=512,
                    )
                    self._write_process(process, remote, item)
                    self._send(
                        list_view,
                        self._LVM_GETITEMTEXTW,
                        row,
                        remote,
                    )
                    value = self._read_process(process, text_address, 1024)
                    values.append(
                        value.decode("utf-16-le", errors="strict").split("\0", 1)[0]
                    )
                rows.append(values)
            return rows

        return self._with_remote_list_memory(process_id, operation)

    def _select_list_row(self, list_view: int, process_id: int, row: int) -> None:
        def operation(process: int, remote: int) -> None:
            item = self._lvitem32(
                mask=self._LVIF_STATE,
                row=row,
                column=0,
                state=self._LVIS_FOCUSED_AND_SELECTED,
                state_mask=self._LVIS_FOCUSED_AND_SELECTED,
            )
            self._write_process(process, remote, item)
            self._send(list_view, self._LVM_SETITEMSTATE, row, remote)

        self._with_remote_list_memory(process_id, operation)

    def _select_combo_index(self, combo: int, index: int) -> None:
        if self._send(combo, self._CB_SETCURSEL, index) != index:
            raise PhotometricMethodGenerationError(
                f"LabSolutions rejected combo index {index}"
            )
        parent = self._user32.GetParent(combo)
        control_id = self._user32.GetDlgCtrlID(combo)
        notification = (self._CBN_SELCHANGE << 16) | (control_id & 0xFFFF)
        self._send(parent, self._WM_COMMAND, notification, combo)

    def _read_wavelengths(
        self, dialog: int, process_id: int
    ) -> PhotometricMethodReadback:
        list_view = self._required_control(dialog, 1182, "SysListView32")
        signal = self._required_control(dialog, 21681, "ComboBox")
        rows = self._list_rows(list_view, process_id)
        if not rows:
            raise PhotometricMethodGenerationError(
                "Photometric method has no registered wavelengths"
            )
        try:
            wavelengths = tuple(float(row[2].strip()) for row in rows)
        except (IndexError, ValueError) as exc:
            raise PhotometricMethodGenerationError(
                f"Cannot parse Photometric wavelength list: {rows}"
            ) from exc
        signal_index = self._send(signal, self._CB_GETCURSEL)
        signal_types = {
            0: "absorbance",
            1: "transmittance",
            2: "reflectance",
            3: "energy",
        }
        if signal_index not in signal_types:
            raise PhotometricMethodGenerationError(
                f"Unsupported Photometric signal index: {signal_index}"
            )
        methods = {row[1].strip() for row in rows}
        if len(methods) != 1 or not next(iter(methods), ""):
            raise PhotometricMethodGenerationError(
                f"Photometric measurement methods are inconsistent: {rows}"
            )
        return PhotometricMethodReadback(
            wavelengths_nm=wavelengths,
            signal_type=signal_types[signal_index],
            signal_label=self._control_text(signal).strip(),
            measurement_method_label=next(iter(methods)),
        )

    def _set_wavelengths(
        self,
        dialog: int,
        process_id: int,
        *,
        wavelengths_nm: list[float],
        signal_type: str,
    ) -> PhotometricMethodReadback:
        if signal_type != "absorbance":
            raise PhotometricMethodGenerationError(
                "Only Photometric absorbance generation is verified"
            )
        if not 1 <= len(wavelengths_nm) <= MAX_VERIFIED_PHOTOMETRIC_WAVELENGTHS:
            raise PhotometricMethodGenerationError(
                "A Photometric method must contain between 1 and "
                f"{MAX_VERIFIED_PHOTOMETRIC_WAVELENGTHS} wavelengths"
            )
        list_view = self._required_control(dialog, 1182, "SysListView32")
        wavelength = self._required_control(dialog, 1156, "Edit")
        column_name = self._required_control(dialog, 1164, "Edit")
        signal = self._required_control(dialog, 21681, "ComboBox")
        method = self._required_control(dialog, 21682, "ComboBox")

        def count() -> int:
            return self._send(list_view, self._LVM_GETITEMCOUNT)

        while count() > 0:
            previous = count()
            self._select_list_row(list_view, process_id, 0)
            self._click(dialog, 5170)
            self._wait_until(
                lambda previous=previous: count() == previous - 1,
                "registered wavelength deletion",
            )
        self._select_combo_index(signal, 0)
        self._select_combo_index(method, 0)
        for value in wavelengths_nm:
            text = f"{value:.4f}".rstrip("0").rstrip(".")
            if "." not in text:
                text += ".0"
            self._set_control_text(wavelength, text)
            self._set_control_text(column_name, f"A{text}")
            previous = count()
            self._click(dialog, 5169)
            self._wait_until(
                lambda previous=previous: count() == previous + 1,
                f"registered wavelength {value:g} nm to be added",
            )
        return self._read_wavelengths(dialog, process_id)

    @staticmethod
    def _matches_photometric(
        readback: PhotometricMethodReadback,
        *,
        wavelengths_nm: list[float],
        signal_type: str,
    ) -> bool:
        return (
            readback.signal_type == signal_type
            and len(readback.wavelengths_nm) == len(wavelengths_nm)
            and all(
                math.isclose(actual, expected, abs_tol=1e-9)
                for actual, expected in zip(
                    readback.wavelengths_nm, wavelengths_nm, strict=True
                )
            )
        )

    def _cleanup(self, window: SpectrumWindow) -> None:
        wavelength_dialog = self._wavelength_dialog(window.process_id)
        if wavelength_dialog is not None:
            try:
                self._click(wavelength_dialog, 2)
                self._wait_closed(
                    wavelength_dialog, "registered-wavelength editor to cancel"
                )
            except Exception:
                pass
        super()._cleanup(window)

    def generate_and_verify(
        self,
        *,
        template_file: Path,
        target_file: Path,
        wavelengths_nm: list[float],
        signal_type: str,
    ) -> PhotometricMethodReadback:
        window, _launched = self.ensure_spectrum_window()
        self._restore(window)
        try:
            self.leave_automatic_control(window)
            self._wait_until(
                lambda: self._user32.IsWindowEnabled(window.handle),
                "Photometric main window to enable after Automatic Control",
                timeout_seconds=self.runtime.startup_timeout_seconds,
            )
            panel = self._connect_instrument(window)
            parameter = self._load_method(panel, window.process_id, template_file)
            wavelength_dialog = self._open_wavelengths(parameter, window.process_id)
            requested = self._set_wavelengths(
                wavelength_dialog,
                window.process_id,
                wavelengths_nm=wavelengths_nm,
                signal_type=signal_type,
            )
            if not self._matches_photometric(
                requested,
                wavelengths_nm=wavelengths_nm,
                signal_type=signal_type,
            ):
                raise PhotometricMethodGenerationError(
                    f"LabSolutions rejected requested wavelengths: {requested.as_dict()}"
                )
            self._click(wavelength_dialog, 1)
            self._wait_closed(
                wavelength_dialog, "registered-wavelength editor to accept"
            )
            self._save_as(parameter, window.process_id, target_file)

            verification = self._load_method(panel, window.process_id, target_file)
            verification_dialog = self._open_wavelengths(
                verification, window.process_id
            )
            readback = self._read_wavelengths(verification_dialog, window.process_id)
            self._click(verification_dialog, 2)
            self._wait_closed(
                verification_dialog, "verified registered-wavelength editor to close"
            )
            self._click(verification, 2)
            self._wait_closed(
                verification, "verified Photometric parameter editor to close"
            )
            if not self._matches_photometric(
                readback,
                wavelengths_nm=wavelengths_nm,
                signal_type=signal_type,
            ):
                raise PhotometricMethodGenerationError(
                    f"Generated Photometric method read-back mismatch: {readback.as_dict()}"
                )
            return readback
        finally:
            self._cleanup(window)


class PhotometricMethodManager(SpectrumMethodManager):
    """Generate one or more attested Photometric methods for a logical request."""

    def __init__(
        self,
        settings: ControlSettings,
        *,
        backend: PhotometricMethodUiBackend | None = None,
        runtime_manager_factory: Callable[[], LabSolutionsRuntimeManager] | None = None,
    ) -> None:
        mode_settings = settings_for_mode(settings, "photometric")
        self.settings = mode_settings
        self.backend = backend or WindowsPhotometricMethodUi(mode_settings)
        self._runtime_manager_factory = runtime_manager_factory or (
            lambda: LabSolutionsRuntimeManager(mode_settings)
        )

    def generate(
        self,
        request: MeasurementRequest,
        *,
        template_name: str | None = None,
    ) -> dict[str, Any]:
        supported, reason = photometric_generation_support(request)
        if not supported:
            raise MeasurementPlanError(reason)
        segment_requests = method_generation_requests(request)
        resolved_segments = [
            resolve_method_template(
                self.settings.method_templates,
                self.settings.generated_method_dir,
                segment,
                template_name=template_name,
            )
            for segment in segment_requests
        ]
        template = resolved_segments[0].template
        targets = [item.generated_method_file.resolve() for item in resolved_segments]
        generated_root = self.settings.generated_method_dir.resolve()
        if any(
            target.parent != generated_root or target.suffix.lower() != ".vphm"
            for target in targets
        ):
            raise PhotometricMethodGenerationError(
                "Generated Photometric methods must be .vphm files directly under "
                f"{generated_root}"
            )
        generated_root.mkdir(parents=True, exist_ok=True)
        if self.settings.data_dir is not None:
            self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        template_sha256 = self._validate_template(template)

        try:
            with InterProcessFileLock(
                self._controller_lock_path,
                timeout=self.settings.lock_timeout_seconds,
                poll_interval=min(self.settings.poll_interval_seconds, 0.1),
            ):
                self._assert_no_active_batch()
                payloads: list[dict[str, Any] | None] = []
                for target, segment in zip(targets, segment_requests, strict=True):
                    payloads.append(
                        self._reuse_existing(
                            target=target,
                            template_sha256=template_sha256,
                            request=segment,
                        )
                    )
                missing = [
                    index for index, payload in enumerate(payloads) if payload is None
                ]
                runtime_before: RuntimeReady | None = None
                runtime_after: RuntimeReady | None = None
                if missing:
                    runtime_before = self._runtime_manager_factory().ensure_ready(
                        allow_reconfigure=True
                    )
                    generation_error: Exception | None = None
                    generated_readbacks: dict[int, PhotometricMethodReadback] = {}
                    try:
                        for index in missing:
                            segment = segment_requests[index]
                            generated_readbacks[index] = (
                                self.backend.generate_and_verify(
                                    template_file=template.method_file,
                                    target_file=targets[index],
                                    wavelengths_nm=list(
                                        segment.parameters["wavelengths_nm"]
                                    ),
                                    signal_type=segment.signal_type,
                                )
                            )
                    except Exception as exc:
                        generation_error = exc
                    try:
                        runtime_after = self._runtime_manager_factory().ensure_ready(
                            allow_reconfigure=False
                        )
                    except Exception as restore_error:
                        if generation_error is not None:
                            raise PhotometricMethodGenerationError(
                                "Photometric method generation failed and Automatic "
                                "Control could not be restored. Generation error: "
                                f"{generation_error}; restore error: {restore_error}"
                            ) from restore_error
                        raise PhotometricMethodGenerationError(
                            "Photometric methods were saved but Automatic Control "
                            f"could not be restored: {restore_error}"
                        ) from restore_error
                    if generation_error is not None:
                        raise PhotometricMethodGenerationError(
                            f"LabSolutions Photometric method generation failed: {generation_error}"
                        ) from generation_error

                    for index in missing:
                        target = targets[index]
                        segment = segment_requests[index]
                        if not target.is_file():
                            raise PhotometricMethodGenerationError(
                                f"LabSolutions did not create generated method: {target}"
                            )
                        if _sha256(template.method_file) != template_sha256:
                            raise PhotometricMethodGenerationError(
                                "The immutable Photometric template changed during generation"
                            )
                        payload: dict[str, Any] = {
                            "schema_version": 1,
                            "status": "generated",
                            "created": True,
                            "mode": "photometric",
                            "segment_index": index + 1,
                            "segment_count": len(segment_requests),
                            "method_file": str(target),
                            "method_sha256": _sha256(target),
                            "template_file": str(template.method_file),
                            "template_sha256": template_sha256,
                            "request": {
                                "signal_type": segment.signal_type,
                                **dict(segment.parameters),
                            },
                            "readback": generated_readbacks[index].as_dict(),
                            "runtime_before": self._runtime_dict(runtime_before),
                            "runtime_after": self._runtime_dict(runtime_after),
                        }
                        write_json_atomic(self._manifest_path(target), payload)
                        payloads[index] = payload

                segments = []
                for index, payload in enumerate(payloads):
                    assert payload is not None
                    if index not in missing:
                        payload = {**payload, "status": "reused", "created": False}
                    segments.append(payload)
                return {
                    "schema_version": 1,
                    "status": "generated" if missing else "reused",
                    "created": bool(missing),
                    "mode": "photometric",
                    "segment_count": len(segments),
                    "method_files": [str(target) for target in targets],
                    "request": {
                        "signal_type": request.signal_type,
                        **dict(request.parameters),
                    },
                    "segments": segments,
                }
        except FileLockTimeoutError as exc:
            raise PhotometricMethodGenerationError(
                "Another process is changing the UV-Vis batch or method state"
            ) from exc


class UVVisMethodManager:
    """Dispatch parameterized method generation to the selected LabSolutions mode."""

    def __init__(self, settings: ControlSettings) -> None:
        self.settings = settings

    def generate(
        self,
        request: MeasurementRequest,
        *,
        template_name: str | None = None,
    ) -> dict[str, Any]:
        if request.mode == "spectrum":
            return SpectrumMethodManager(
                settings_for_mode(self.settings, "spectrum")
            ).generate(request, template_name=template_name)
        if request.mode == "photometric":
            return PhotometricMethodManager(self.settings).generate(
                request, template_name=template_name
            )
        supported, reason = method_generation_support(request)
        assert not supported
        raise MeasurementPlanError(reason)
