"""Exercise the Rust picker with fake fzf and Rex processes."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "bin/rex-popup"


class PopupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rex popup tests ")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.assertTrue(BINARY.is_file(), "Run make build first")
        self.input_log = self.root / "input.bin"
        self.args_log = self.root / "fzf-args.json"
        self.rex_log = self.root / "rex-args.json"
        self.calls_log = self.root / "rex-calls.jsonl"
        self.view = {}
        self.result_file = self.root / "result.json"
        self.fzf = self.root / "fake fzf"
        self.fzf.write_text("#!" + sys.executable + "\n" + '''
import json, os, sys
from pathlib import Path
config = json.loads(os.environ["POPUP_STUB"])
Path(config["args_log"]).write_text(json.dumps({"args": sys.argv[1:], "options": os.getenv("FZF_DEFAULT_OPTS"), "options_file": os.getenv("FZF_DEFAULT_OPTS_FILE")}))
key = config.get("key", "tab")
id = config.get("id", "1")
record = id + "\\t" + "display decoration" + "x" * config.get("early_output", 0)
output = (key + "\\0" + record + "\\0").encode()
if config.get("early_output"):
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()
Path(config["input_log"]).write_bytes(sys.stdin.buffer.read())
if not config.get("early_output") and config.get("exit", 0) == 0:
    sys.stdout.buffer.write(output)
sys.exit(config.get("exit", 0))
''')
        self.fzf.chmod(0o755)
        self.rex = self.root / "fake rex"
        self.rex.write_text("#!" + sys.executable + "\n" + '''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
Path(os.environ["POPUP_REX_LOG"]).write_text(json.dumps(args))
with Path(os.environ["POPUP_REX_CALLS_LOG"]).open("a") as log:
    print(json.dumps(args), file=log)
method = args[args.index("call") + 1]
print(os.environ["POPUP_REX_VIEW"] if method == "session.view" else "{}")
sys.exit(int(os.getenv("POPUP_REX_EXIT", "0")))
''')
        self.rex.chmod(0o755)

    def state(self, value="selected-token"):
        return {"items": [{"id": "1", "title": value, "subtitle": "1: tab / source pane"}],
                "values": {"1": value}, "fzf": str(self.fzf), "rex": str(self.rex),
                "target": {"server": "remote box", "session_id": "session:origin", "block_id": "block:origin"},
                "clipboard": "osc52", "query": "initial query", "scope": "tab",
                "result_file": str(self.result_file)}

    def run_popup(self, state=None, **stub):
        directory = self.root / "state folder"
        directory.mkdir(exist_ok=True)
        path = directory / "state.json"
        path.write_text(json.dumps(state or self.state(), ensure_ascii=False))
        env = os.environ.copy()
        env["POPUP_STUB"] = json.dumps({"input_log": str(self.input_log), "args_log": str(self.args_log), **stub})
        env["POPUP_REX_LOG"] = str(self.rex_log)
        env["POPUP_REX_CALLS_LOG"] = str(self.calls_log)
        env["POPUP_REX_VIEW"] = json.dumps(self.view)
        env["REX_BLOCK"] = "block:picker"
        env["FZF_DEFAULT_OPTS"] = "--multi --expect=ctrl-x"
        env["FZF_DEFAULT_OPTS_FILE"] = "/a/user/options/file"
        result = subprocess.run([str(BINARY), str(path)], env=env, capture_output=True, timeout=20)
        self.assertFalse(path.exists())
        self.assertFalse(directory.exists())
        return result

    def test_tab_copies_exact_utf8_with_osc52(self):
        value = '跨 tab 插入：echo  "你好"\t/path with spaces\nsecond line'
        result = self.run_popup(self.state(value))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith(b"\x1b]2;rex-extrakto\x07"))
        encoded = result.stdout.split(b"\x1b]52;c;", 1)[1]
        self.assertTrue(encoded.endswith(b"\x07"))
        self.assertEqual(base64.b64decode(encoded[:-1]), value.encode())
        outcome = json.loads(self.result_file.read_text())
        self.assertEqual(outcome, {"status": "copied", "text": value, "count": 1})
        self.assertFalse(self.rex_log.exists())

    def test_display_separators_do_not_change_exact_values(self):
        value = "a\tb\nc\rd\0e"
        self.assertEqual(self.run_popup(self.state(value)).returncode, 0)
        data = self.input_log.read_bytes()
        self.assertIn("a ⇥ b ⏎ c ⏎ d␀e".encode(), data)
        self.assertEqual(data.count(b"\0"), 1)
        self.assertEqual(json.loads(self.result_file.read_text())["text"], value)

    def test_fzf_options_are_isolated_and_query_is_literal(self):
        state = self.state()
        state["query"] = 'literal "quote" $(echo untouched)'
        self.assertEqual(self.run_popup(state).returncode, 0)
        logged = json.loads(self.args_log.read_text())
        self.assertIsNone(logged["options"])
        self.assertIsNone(logged["options_file"])
        self.assertIn("--query=" + state["query"], logged["args"])
        self.assertIn("--prompt=Tab extract> ", logged["args"])
        self.assertIn("--expect=tab,enter", logged["args"])

    def test_enter_sends_exact_paste_to_original_target(self):
        value = 'echo "你好"  --path=/tmp/with spaces'
        result = self.run_popup(self.state(value), key="enter")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(self.rex_log.read_text())
        self.assertEqual(args[:5], ["-S", "remote box", "api", "call", "block.event"])
        self.assertEqual(json.loads(args[5]), {"session_id": "session:origin", "block_id": "block:origin",
                         "event": {"type": "paste", "payload": {"text": value}}})
        self.assertNotIn(b"\x1b]52;", result.stdout)
        self.assertEqual(json.loads(self.result_file.read_text())["status"], "inserted")

    def test_escape_and_no_match_cancel_without_side_effects(self):
        for code in (130, 1):
            with self.subTest(code=code):
                result = self.run_popup(exit=code)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(self.result_file.read_text()), {"status": "cancelled"})
                self.assertFalse(self.rex_log.exists())
                self.assertNotIn(b"\x1b]52;", result.stdout)

    def test_fzf_failure_cleans_temporary_state(self):
        result = self.run_popup(exit=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"fzf exited", result.stderr)
        self.assertFalse(self.result_file.exists())

    def test_unknown_candidate_cannot_insert(self):
        result = self.run_popup(key="enter", id="unknown")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.rex_log.exists())
        self.assertFalse(self.result_file.exists())

    def test_unexpected_key_cannot_copy_or_insert(self):
        result = self.run_popup(key="ctrl-x")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.rex_log.exists())
        self.assertNotIn(b"\x1b]52;", result.stdout)

    def test_large_input_and_output_are_drained_concurrently(self):
        state = self.state()
        state["items"] = [{"id": str(i), "title": "x" * 65536} for i in range(1, 101)]
        state["values"] = {str(i): "value-" + str(i) for i in range(1, 101)}
        result = self.run_popup(state, early_output=200000)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(self.input_log.stat().st_size, 6_000_000)
        self.assertEqual(json.loads(self.result_file.read_text())["text"], "value-1")

    def test_candidate_without_exact_value_is_rejected(self):
        state = self.state()
        state["values"] = {}
        result = self.run_popup(state)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.input_log.exists())
        self.assertFalse(self.rex_log.exists())

    def zoomed_state(self):
        state = self.state()
        state["restore_zoom"] = {"server": "unix:///picker.sock", "session_id": "session:ui",
                                 "window_id": "window:origin", "block_id": "block:origin"}
        self.view = {"active_window_id": "window:origin", "windows": [
            {"window_id": "window:origin", "focused_block_id": "block:picker", "layers": [
                {"blocks": [{"block_id": "block:origin"}, {"block_id": "block:peer"}]},
                {"blocks": [{"block_id": "block:picker"}]},
            ]},
            {"window_id": "window:other", "focused_block_id": "block:other", "layers": [{"blocks": []}]},
        ]}
        return state

    def calls(self):
        return [json.loads(line) for line in self.calls_log.read_text().splitlines()]

    def test_floating_picker_restores_zoom_after_copy_and_cancel(self):
        for key, code in [("tab", 0), ("escape", 130)]:
            with self.subTest(key=key):
                self.calls_log.unlink(missing_ok=True)
                result = self.run_popup(self.zoomed_state(), key=key, exit=code)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = self.calls()
                self.assertEqual([call[4] for call in calls], ["session.view", "session.zoom_block"])
                self.assertTrue(all(call[:2] == ["-S", "unix:///picker.sock"] for call in calls))
                self.assertEqual(json.loads(calls[1][5]), {"session_id": "session:ui", "block_id": "block:origin", "zoom": True})

    def test_insert_finishes_before_restoring_zoom_on_its_ui_server(self):
        result = self.run_popup(self.zoomed_state(), key="enter")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual([call[4] for call in calls], ["block.event", "session.view", "session.zoom_block"])
        self.assertEqual(calls[0][:2], ["-S", "remote box"])
        self.assertEqual(calls[1][:2], ["-S", "unix:///picker.sock"])

    def test_fzf_failure_restores_zoom_before_reporting_the_error(self):
        result = self.run_popup(self.zoomed_state(), exit=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"fzf exited", result.stderr)
        self.assertEqual([call[4] for call in self.calls()], ["session.view", "session.zoom_block"])
        self.assertFalse(self.result_file.exists())

    def test_restore_respects_focus_changes_and_source_removal(self):
        for change in ["other_tab", "other_pane", "closed_source", "closed_window", "moved_source"]:
            with self.subTest(change=change):
                state = self.zoomed_state()
                self.calls_log.unlink(missing_ok=True)
                if change == "other_tab":
                    self.view["active_window_id"] = "window:other"
                elif change == "other_pane":
                    self.view["windows"][0]["focused_block_id"] = "block:peer"
                elif change == "closed_window":
                    self.view["windows"].pop(0)
                else:
                    block = self.view["windows"][0]["layers"][0]["blocks"].pop(0)
                    if change == "moved_source":
                        self.view["windows"][1]["layers"][0]["blocks"].append(block)
                result = self.run_popup(state, exit=130)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([call[4] for call in self.calls()], ["session.view"])

    def test_normal_use_does_not_write_a_result_file(self):
        state = self.state()
        del state["result_file"]
        result = self.run_popup(state)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.result_file.exists())
        self.assertFalse(self.calls_log.exists())


if __name__ == "__main__":
    unittest.main()
