"""Exercise the Rust picker with fake fzf and Rex processes."""
import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
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

    def test_cancellation_restores_focus_without_enabling_zoom(self):
        state = self.zoomed_state()
        state["restore_focus"] = state.pop("restore_zoom")
        result = self.run_popup(state, exit=130)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual([call[4] for call in calls], ["session.view", "session.focus_block"])
        self.assertEqual(json.loads(calls[-1][5]), {"session_id": "session:ui", "block_id": "block:origin"})

    def test_original_zoom_takes_priority_over_the_focus_target(self):
        state = self.zoomed_state()
        state["restore_focus"] = {**state["restore_zoom"], "block_id": "block:peer"}
        result = self.run_popup(state, exit=130)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.calls()[-1][5])["block_id"], "block:origin")
        self.assertEqual(self.calls()[-1][4], "session.zoom_block")

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


class LifecycleWatcherTests(unittest.TestCase):
    """Drive the detached lifecycle watcher with a scripted fake Rex process."""

    SESSION = "session:ui"
    WINDOW = "window:origin"
    OTHER_WINDOW = "window:other"
    PICKER = "block:picker"
    SOURCE = "block:origin"
    PEER = "block:peer"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rex watcher tests ")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.assertTrue(BINARY.is_file(), "Run make build first")
        self.calls_log = self.root / "rex-calls.jsonl"
        self.view_file = self.root / "view.json"
        self.script_file = self.root / "events.json"
        self.started_file = self.root / "events-started"
        self.result_file = self.root / "result.json"
        self.state_index = 0
        self.processes = []
        self.addCleanup(self.stop_processes)
        self.rex = self.root / "fake rex"
        self.rex.write_text("#!" + sys.executable + "\n" + r'''
import json, os, sys, time
from pathlib import Path

args = sys.argv[1:]
calls_log = Path(os.environ["POPUP_REX_CALLS_LOG"])

def log(mode, method=None, request=None):
    with calls_log.open("a") as stream:
        stream.write(json.dumps({"args": args, "mode": mode, "method": method, "request": request}) + "\n")
        stream.flush()

def wait_for(predicate):
    while not predicate():
        time.sleep(0.005)

def view_calls():
    if not calls_log.exists():
        return 0
    count = 0
    for line in calls_log.read_text().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("method") == "session.view":
            count += 1
    return count

if "events" in args:
    log("events")
    Path(os.environ["WATCH_STARTED"]).write_text("started\n")
    for step in json.loads(Path(os.environ["WATCH_SCRIPT"]).read_text()):
        if "wait_file" in step:
            wait_for(lambda path=step["wait_file"]: Path(path).exists())
        elif "wait_missing" in step:
            wait_for(lambda path=step["wait_missing"]: not Path(path).exists())
        elif "wait_calls" in step:
            wait_for(lambda count=step["wait_calls"]: view_calls() >= count)
        elif "view" in step:
            path = Path(os.environ["WATCH_VIEW"])
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(step["view"]))
            os.replace(temporary, path)
        elif "emit" in step:
            sys.stdout.write(json.dumps(step["emit"], separators=(",", ":")) + "\n")
            sys.stdout.flush()
        elif "eof" in step:
            break
    sys.exit(0)

if "api" in args:
    index = args.index("call")
    method = args[index + 1]
    request = json.loads(args[index + 2]) if index + 2 < len(args) else None
    log("api", method, request)
    if method == "session.view":
        path = Path(os.environ["WATCH_VIEW"])
        sys.stdout.write(path.read_text() if path.is_file() else os.environ.get("POPUP_REX_VIEW", "{}"))
    else:
        sys.stdout.write("{}")
    sys.exit(0)

log("other")
sys.stdout.write(os.environ.get("POPUP_REX_VIEW", "{}"))
''')
        self.rex.chmod(0o755)
        self.view_file.write_text("{}")

    def stop_processes(self):
        for process in self.processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def environment(self):
        environment = os.environ.copy()
        environment["POPUP_REX_CALLS_LOG"] = str(self.calls_log)
        environment["POPUP_REX_VIEW"] = "{}"
        environment["WATCH_SCRIPT"] = str(self.script_file)
        environment["WATCH_VIEW"] = str(self.view_file)
        environment["WATCH_STARTED"] = str(self.started_file)
        return environment

    def watch_state(self, restore="zoom", result=False):
        state = {
            "items": [{"id": "1", "title": "token", "subtitle": "1: tab / source pane"}],
            "values": {"1": "token"},
            "fzf": str(self.rex),
            "rex": str(self.rex),
            "target": {"server": "unix:///picker.sock", "session_id": self.SESSION,
                       "block_id": self.SOURCE},
            "clipboard": "osc52",
            "query": "",
            "scope": "tab",
            "watch": {"server": "unix:///picker.sock", "session_id": self.SESSION,
                      "window_id": self.WINDOW},
        }
        target = {"server": "unix:///picker.sock", "session_id": self.SESSION,
                  "window_id": self.WINDOW, "block_id": self.SOURCE}
        if restore == "zoom":
            state["restore_zoom"] = target
        elif restore == "focus":
            state["restore_focus"] = target
        if result:
            state["result_file"] = str(self.result_file)
        return state

    def write_state(self, state):
        self.state_index += 1
        directory = self.root / ("watch state " + str(self.state_index))
        directory.mkdir()
        path = directory / "state.json"
        path.write_text(json.dumps(state))
        return path

    def write_script(self, script):
        self.script_file.write_text(json.dumps(script))

    def write_block(self, state_path):
        # Match the production atomic handoff to the watcher.
        block_path = state_path.with_suffix(".block")
        temporary = block_path.with_name(block_path.name + ".tmp")
        temporary.write_text(self.PICKER)
        os.replace(temporary, block_path)

    def reset(self):
        for path in (self.calls_log, self.started_file, self.result_file):
            path.unlink(missing_ok=True)
        self.script_file.unlink(missing_ok=True)
        self.view_file.write_text("{}")

    def start_watch(self, state_path):
        process = subprocess.Popen(
            [str(BINARY), "--watch", str(state_path)],
            env=self.environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        self.processes.append(process)
        return process

    def start_wait_ready(self, state_path):
        process = subprocess.Popen(
            [str(BINARY), "--wait-ready", str(state_path)],
            env=self.environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        self.processes.append(process)
        return process

    def wait_ready(self, state_path):
        return subprocess.run(
            [str(BINARY), "--wait-ready", str(state_path)],
            env=self.environment(), capture_output=True, timeout=20)

    def wait_for_file(self, path):
        deadline = time.monotonic() + 10
        while not path.exists():
            if time.monotonic() >= deadline:
                self.fail(path.name + " did not appear")
            time.sleep(0.01)

    def finish(self, process):
        try:
            stdout, stderr = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            self.fail("watcher did not exit")
        return process.returncode, stdout, stderr

    def calls(self):
        if not self.calls_log.exists():
            return []
        return [json.loads(line) for line in self.calls_log.read_text().splitlines() if line.strip()]

    def api_calls(self):
        return [call for call in self.calls() if call["mode"] == "api"]

    def assert_cleaned(self, state_path):
        self.assertFalse(state_path.exists())
        for extension in ("ready", "block", "finished"):
            self.assertFalse(state_path.with_suffix("." + extension).exists())
        self.assertFalse(state_path.parent.exists())

    def envelope(self, event_type, payload, session=None):
        return {"type": "session_event",
                "payload": {"session_id": session or self.SESSION,
                            "event": {"type": event_type, "payload": payload}}}

    def subscribed(self, session=None):
        return self.envelope("client_connected", {
            "client_id": "client:test",
            "principal": {"kind": "peer", "transport": "unix", "display": "test"},
        }, session)

    def view(self, revision, active=None, focused=None, origin=None, other=None):
        origin = (self.SOURCE, self.PEER) if origin is None else origin
        other = () if other is None else other
        return {
            "revision": revision,
            "session_id": self.SESSION,
            "active_window_id": active or self.WINDOW,
            "windows": [
                {"window_id": self.WINDOW, "focused_block_id": focused or self.SOURCE,
                 "layers": [{"blocks": [{"block_id": block} for block in origin]}]},
                {"window_id": self.OTHER_WINDOW, "focused_block_id": "block:other",
                 "layers": [{"blocks": [{"block_id": block} for block in other]}]},
            ],
        }

    def close_script(self, state_path, view, extra=(), revision=None):
        return [
            {"emit": self.subscribed()},
            {"wait_file": str(state_path.with_suffix(".block"))},
            *extra,
            {"emit": self.envelope("block_closed", {"block_id": self.PICKER})},
            {"view": view},
            {"emit": self.envelope("session_view_changed",
                                   {"revision": view["revision"] if revision is None else revision})},
        ]

    def test_direct_close_restores_and_cleans_state(self):
        for restore, method in (("zoom", "session.zoom_block"), ("focus", "session.focus_block")):
            with self.subTest(restore=restore):
                self.reset()
                state_path = self.write_state(self.watch_state(restore=restore, result=True))
                self.write_script(self.close_script(
                    state_path, self.view(2, focused=self.SOURCE, origin=(self.SOURCE, self.PEER))))
                watcher = self.start_watch(state_path)
                self.assertEqual(self.wait_ready(state_path).returncode, 0)
                self.write_block(state_path)
                returncode, stdout, stderr = self.finish(watcher)
                self.assertEqual(returncode, 0, stderr.decode())
                calls = self.api_calls()
                self.assertEqual([call["method"] for call in calls], ["session.view", method])
                self.assertEqual(calls[0]["request"], {"session_id": self.SESSION})
                self.assertEqual(calls[0]["args"][:2], ["-S", "unix:///picker.sock"])
                expected = {"session_id": self.SESSION, "block_id": self.SOURCE}
                if restore == "zoom":
                    expected["zoom"] = True
                self.assertEqual(calls[1]["request"], expected)
                self.assertEqual(calls[1]["args"][:2], ["-S", "unix:///picker.sock"])
                self.assertEqual(json.loads(self.result_file.read_text()), {"status": "cancelled"})
                self.assert_cleaned(state_path)
                events = [call for call in self.calls() if call["mode"] == "events"]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["args"],
                                 ["-S", "unix:///picker.sock", "--autostart=false",
                                  "events", "-s", self.SESSION, "--json"])

    def test_direct_close_after_state_removal_still_restores(self):
        self.reset()
        state_path = self.write_state(self.watch_state(result=True))
        script = self.close_script(
            state_path, self.view(2, focused=self.SOURCE, origin=(self.SOURCE, self.PEER)))
        script.insert(2, {"wait_missing": str(state_path)})
        self.write_script(script)
        watcher = self.start_watch(state_path)
        self.assertEqual(self.wait_ready(state_path).returncode, 0)
        self.write_block(state_path)
        # A popup Drop can remove state.json before the watcher handles the close.
        state_path.unlink()
        returncode, stdout, stderr = self.finish(watcher)
        self.assertEqual(returncode, 0, stderr.decode())
        calls = self.api_calls()
        self.assertEqual([call["method"] for call in calls], ["session.view", "session.zoom_block"])
        self.assertEqual(calls[1]["request"],
                         {"session_id": self.SESSION, "block_id": self.SOURCE, "zoom": True})
        self.assertEqual(json.loads(self.result_file.read_text()), {"status": "cancelled"})
        self.assert_cleaned(state_path)

    def test_focus_switch_before_close_does_not_restore(self):
        for change in ("tab", "pane"):
            with self.subTest(change=change):
                self.reset()
                state_path = self.write_state(self.watch_state())
                if change == "tab":
                    # The view is deliberately stale so the active_window_changed
                    # event, not the snapshot, has to suppress the restore.
                    script = self.close_script(
                        state_path,
                        self.view(2, focused=self.SOURCE, origin=(self.SOURCE, self.PEER)),
                        extra=[{"emit": self.envelope(
                            "active_window_changed", {"window_id": self.OTHER_WINDOW})}])
                else:
                    script = self.close_script(
                        state_path,
                        self.view(2, focused=self.SOURCE, origin=(self.SOURCE, self.PEER)),
                        extra=[
                            {"view": self.view(1, focused=self.PEER,
                                               origin=(self.SOURCE, self.PEER, self.PICKER))},
                            {"emit": self.envelope("session_view_changed", {"revision": 1})},
                            {"wait_calls": 1},
                        ])
                self.write_script(script)
                watcher = self.start_watch(state_path)
                self.assertEqual(self.wait_ready(state_path).returncode, 0)
                self.write_block(state_path)
                returncode, stdout, stderr = self.finish(watcher)
                self.assertEqual(returncode, 0, stderr.decode())
                methods = [call["method"] for call in self.api_calls()]
                self.assertEqual(methods, ["session.view"] if change == "tab"
                                 else ["session.view", "session.view"])
                self.assertNotIn("session.zoom_block", methods)
                self.assertNotIn("session.focus_block", methods)
                self.assert_cleaned(state_path)

    def test_closed_or_moved_source_does_not_restore(self):
        for change in ("closed", "moved"):
            with self.subTest(change=change):
                self.reset()
                state_path = self.write_state(self.watch_state())
                other = (self.SOURCE,) if change == "moved" else ()
                view = self.view(2, focused=self.PEER, origin=(self.PEER,), other=other)
                self.write_script(self.close_script(state_path, view))
                watcher = self.start_watch(state_path)
                self.assertEqual(self.wait_ready(state_path).returncode, 0)
                self.write_block(state_path)
                returncode, stdout, stderr = self.finish(watcher)
                self.assertEqual(returncode, 0, stderr.decode())
                methods = [call["method"] for call in self.api_calls()]
                self.assertEqual(methods, ["session.view"])
                self.assert_cleaned(state_path)

    def test_newer_revision_after_close_does_not_restore(self):
        self.reset()
        state_path = self.write_state(self.watch_state())
        view = self.view(3, focused=self.SOURCE, origin=(self.SOURCE, self.PEER))
        self.write_script(self.close_script(state_path, view, revision=2))
        watcher = self.start_watch(state_path)
        self.assertEqual(self.wait_ready(state_path).returncode, 0)
        self.write_block(state_path)
        returncode, stdout, stderr = self.finish(watcher)
        self.assertEqual(returncode, 0, stderr.decode())
        methods = [call["method"] for call in self.api_calls()]
        self.assertEqual(methods, ["session.view"])
        self.assert_cleaned(state_path)

    def test_normal_exit_handshake_skips_duplicate_restore(self):
        self.reset()
        state_path = self.write_state(self.watch_state(result=True))
        self.write_script([
            {"emit": self.subscribed()},
            {"wait_file": str(state_path.with_suffix(".block"))},
            {"wait_file": str(state_path.with_suffix(".finished"))},
            {"wait_missing": str(state_path)},
            {"emit": self.envelope("block_event", {"block_id": self.PICKER,
                "name": "child_exited", "payload": {"exit_code": 0}})},
        ])
        watcher = self.start_watch(state_path)
        self.assertEqual(self.wait_ready(state_path).returncode, 0)
        self.write_block(state_path)
        outcome = {"status": "copied", "text": "token", "count": 1}
        self.result_file.write_text(json.dumps(outcome) + "\n")
        # The popup checked that the picker was still in its view, restored it,
        # wrote its outcome, and removed state.json.
        state_path.with_suffix(".finished").write_text("finished\n")
        state_path.unlink()
        returncode, stdout, stderr = self.finish(watcher)
        self.assertEqual(returncode, 0, stderr.decode())
        self.assertEqual([call["method"] for call in self.api_calls()], ["block.close"])
        self.assertEqual(json.loads(self.result_file.read_text()), outcome)
        self.assert_cleaned(state_path)

    def test_completion_keeps_diagnostic_and_failed_panes(self):
        for keep_open, marker in [(True, "finished\n"), (False, "failed\n")]:
            with self.subTest(keep_open=keep_open, marker=marker):
                self.reset()
                state = self.watch_state()
                state["watch"]["keep_open"] = keep_open
                state_path = self.write_state(state)
                self.write_script([
                    {"emit": self.subscribed()},
                    {"wait_file": str(state_path.with_suffix(".finished"))},
                    {"emit": self.envelope("block_event", {"block_id": self.PICKER,
                        "name": "child_exited", "payload": {"exit_code": 0}})},
                ])
                watcher = self.start_watch(state_path)
                self.assertEqual(self.wait_ready(state_path).returncode, 0)
                self.write_block(state_path)
                state_path.with_suffix(".finished").write_text(marker)
                returncode, _, stderr = self.finish(watcher)
                self.assertEqual(returncode, 0, stderr.decode())
                self.assertEqual(self.api_calls(), [])
                self.assert_cleaned(state_path)

    def test_failed_subscription_cleans_state(self):
        cases = {
            "eof": [],
            "wrong_session": [{"emit": self.subscribed(session="session:other")}],
        }
        for name, script in cases.items():
            with self.subTest(case=name):
                self.reset()
                state_path = self.write_state(self.watch_state())
                self.write_script(script)
                watcher = self.start_watch(state_path)
                returncode, stdout, stderr = self.finish(watcher)
                self.assertNotEqual(returncode, 0)
                self.assertIn(b"Rex event stream ended before subscription", stderr)
                self.assert_cleaned(state_path)

    def test_wait_ready_gates_on_subscription(self):
        self.reset()
        state_path = self.write_state(self.watch_state())
        gate = self.root / "subscription gate"
        script = self.close_script(
            state_path, self.view(2, focused=self.SOURCE, origin=(self.SOURCE, self.PEER)))
        script.insert(0, {"wait_file": str(gate)})
        self.write_script(script)
        watcher = self.start_watch(state_path)
        ready = self.start_wait_ready(state_path)
        self.wait_for_file(self.started_file)
        self.assertIsNone(ready.poll(), "wait-ready returned before subscription")
        self.assertFalse(state_path.with_suffix(".ready").exists())
        gate.write_text("open\n")
        _, ready_error = ready.communicate(timeout=20)
        self.assertEqual(ready.returncode, 0, ready_error.decode())
        self.assertTrue(state_path.with_suffix(".ready").is_file())
        self.write_block(state_path)
        returncode, stdout, stderr = self.finish(watcher)
        self.assertEqual(returncode, 0, stderr.decode())
        self.assertEqual([call["method"] for call in self.api_calls()],
                         ["session.view", "session.zoom_block"])
        self.assert_cleaned(state_path)

    def test_wait_ready_reports_early_watcher_exit(self):
        self.reset()
        state_path = self.write_state(self.watch_state())
        self.write_script([])
        watcher = self.start_watch(state_path)
        result = self.wait_ready(state_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"exited before it was ready", result.stderr)
        returncode, stdout, stderr = self.finish(watcher)
        self.assertNotEqual(returncode, 0)
        self.assertIn(b"Rex event stream ended before subscription", stderr)
        self.assert_cleaned(state_path)

    def test_empty_handoff_waits_for_the_complete_picker_id(self):
        self.reset()
        state_path = self.write_state(self.watch_state())
        state_path.with_suffix(".block").write_text("")
        gate = self.root / "handoff complete"
        script = self.close_script(
            state_path, self.view(2, focused=self.SOURCE, origin=(self.SOURCE, self.PEER)))
        script.insert(1, {"wait_file": str(gate)})
        self.write_script(script)
        watcher = self.start_watch(state_path)
        ready = self.wait_ready(state_path)
        self.assertEqual(ready.returncode, 0, ready.stderr.decode())
        self.write_block(state_path)
        gate.write_text("complete\n")
        returncode, _, stderr = self.finish(watcher)
        self.assertEqual(returncode, 0, stderr.decode())
        self.assertEqual([call["method"] for call in self.api_calls()],
                         ["session.view", "session.zoom_block"])
        self.assert_cleaned(state_path)


if __name__ == "__main__":
    unittest.main()
