"""Check portable clipboard output without writing to the host clipboard."""
import base64
import importlib.util
import io
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("popup", ROOT / "scripts/popup.py")
popup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(popup)


class ClipboardTests(unittest.TestCase):
    def test_osc52_sends_exact_utf8_without_a_newline(self):
        raw = io.BytesIO()
        output = io.TextIOWrapper(raw, encoding="utf-8", write_through=True)
        value = '跨 tab 插入：echo  "你好"\t/path with spaces'
        with mock.patch.object(popup.sys, "stdout", output):
            popup.copy_text(value, "osc52")
        encoded = raw.getvalue()
        self.assertTrue(encoded.startswith(b"\x1b]52;c;") and encoded.endswith(b"\x07"))
        self.assertEqual(base64.b64decode(encoded[7:-1]), value.encode())

    def test_linux_auto_uses_osc52(self):
        raw = io.BytesIO()
        output = io.TextIOWrapper(raw, encoding="utf-8", write_through=True)
        with mock.patch.object(popup.sys, "platform", "linux"), mock.patch.object(popup.sys, "stdout", output):
            popup.copy_text("selected-token")
        self.assertEqual(raw.getvalue(), b"\x1b]52;c;" + base64.b64encode(b"selected-token") + b"\x07")

    def test_mac_auto_keeps_pbcopy(self):
        with mock.patch.object(popup.sys, "platform", "darwin"), mock.patch.object(popup.subprocess, "run") as run:
            popup.copy_text("selected-token")
        run.assert_called_once_with(["/usr/bin/pbcopy"], input=b"selected-token", check=True)


if __name__ == "__main__":
    unittest.main()
