import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from shimadzu_uvvis.method_manager import WindowsSpectrumMethodUi, SpectrumMethodGenerationError
from shimadzu_uvvis.runtime_manager import SpectrumWindow


class HiddenSpectrumPanelTests(unittest.TestCase):
    def ui(self, connected=True):
        ui=WindowsSpectrumMethodUi.__new__(WindowsSpectrumMethodUi)
        visible={10:False}
        texts={10:'仪器控制面板',20:'700.00 nm' if connected else '-----'}
        ui._windows=lambda **_: [10]
        ui._window_text=lambda handle: texts.get(handle,'')
        ui._control=lambda handle, ident, kind: {11002:20,1638:21}.get(ident) if handle == 10 else None
        ui._user32=SimpleNamespace(IsWindowEnabled=lambda _:True,
            IsWindowVisible=lambda handle:visible.get(handle,False),
            ShowWindow=Mock(side_effect=lambda handle,_:visible.update({handle:True})))
        def wait(predicate, *args, **kwargs):
            value=predicate()
            self.assertTrue(value, args)
            return value
        ui._wait_until=wait
        ui.runtime=SimpleNamespace(startup_timeout_seconds=1)
        ui._initialization_dialog=lambda _:None
        ui._find_menu_command=Mock(return_value=100)
        ui._post=Mock(side_effect=lambda *args:texts.update({20:'700.00 nm'}))
        return ui

    def test_hidden_connected_panel_is_shown_without_reconnecting(self):
        ui=self.ui()
        self.assertEqual(ui._connect_instrument(SpectrumWindow(1,2,'Spectrum')),10)
        ui._find_menu_command.assert_not_called()
        ui._post.assert_not_called()
        ui._user32.ShowWindow.assert_called_once_with(10,5)

    def test_disconnected_panel_still_connects_once(self):
        ui=self.ui(connected=False)
        self.assertEqual(ui._connect_instrument(SpectrumWindow(1,2,'Spectrum')),10)
        ui._find_menu_command.assert_called_once()
        ui._post.assert_called_once()

    def test_ambiguous_panels_are_not_operated(self):
        ui=self.ui()
        ui._windows=lambda **_: [10,11]
        ui._window_text=lambda _: 'Instrument Control Panel'
        ui._control=lambda *args:20
        with self.assertRaises(SpectrumMethodGenerationError): ui._instrument_panel(1)
        ui._user32.ShowWindow.assert_not_called()


if __name__ == '__main__': unittest.main()
