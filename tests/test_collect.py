"""Test the real async collector with an isolated, controllable capture CLI."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COLLECT = ROOT / "bin/rex-collect"
EXTRACT = ROOT / "bin/rex-extract"
FAKE = r'''
import json
import os
from pathlib import Path
import sys
import time

root = Path(__file__).parent
fixture = json.loads((root / "fixture.json").read_text())
args = sys.argv[1:]
if args[:1] == ["-S"]:
    assert args[1] == fixture["server"]
    args = args[2:]
assert args[:2] == ["capture", "-s"] and args[2] == "test-session"
assert args[3] == "-b" and args[5:] == ["--trim", "--unwrap"]
block = args[4]
data = fixture["panes"][block]
def log(kind):
    event = json.dumps({"kind": kind, "block": block, "pid": os.getpid(), "at": time.monotonic_ns()}) + "\n"
    fd = os.open(root / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, event.encode())
    finally:
        os.close(fd)
log("start")
try:
    time.sleep(data.get("delay", 0))
    if "error" in data:
        sys.stderr.write(data["error"])
        sys.exit(3)
    sys.stdout.buffer.write(bytes(data["bytes"]) if "bytes" in data else data.get("text", "").encode())
    sys.stdout.buffer.flush()
    sys.stderr.write(data.get("stderr", ""))
finally:
    log("end")
'''


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rex collect tests ")
        self.root = Path(self.temp.name)
        self.fake = self.root / "capture cli.py"
        self.fake.write_text("#!" + sys.executable + "\n" + FAKE)
        self.fake.chmod(0o755)

    def events(self):
        path = self.root / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def tearDown(self):
        for event in self.events():
            if event["kind"] == "start":
                command = subprocess.run(["ps", "-p", str(event["pid"]), "-o", "args="], capture_output=True).stdout.decode()
                if str(self.fake) in command:
                    try:
                        os.kill(event["pid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        self.temp.cleanup()

    def invoke(self, fixture_panes, **options):
        fixture = {"panes": fixture_panes, "server": options.get("server")}
        (self.root / "fixture.json").write_text(json.dumps(fixture))
        (self.root / "events.jsonl").unlink(missing_ok=True)
        request = {"rex": str(self.fake), "session_id": "test-session",
                   "panes": [{"block_id": block, "display": block} for block in fixture_panes]}
        request.update(options)
        return subprocess.run([str(COLLECT)], input=json.dumps(request).encode(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8)

    def peak(self):
        active = peak = 0
        for event in sorted(self.events(), key=lambda event: event["at"]):
            active += 1 if event["kind"] == "start" else -1
            peak = max(peak, active)
        return peak

    def assert_children_stopped(self):
        for event in self.events():
            if event["kind"] == "start":
                state = subprocess.run(["ps", "-p", str(event["pid"]), "-o", "stat="], capture_output=True).stdout.decode().strip()
                self.assertTrue(not state or state.startswith("Z"), (event, state))

    def test_bounded_parallelism_preserves_input_order_and_outputs(self):
        panes = {f"pane-{i}": {"delay": 0.16 if i == 0 else 0.03,
                              "text": f"older-token\n中文-{i} https://example.com/a_(b)?x=1&y=two\n"}
                 for i in range(4)}
        process = self.invoke(panes, jobs=2, extract_jobs=1)
        self.assertEqual(process.returncode, 0, process.stderr)
        output = json.loads(process.stdout)["panes"]
        self.assertEqual([item["block_id"] for item in output], list(panes))
        self.assertEqual(self.peak(), 2)
        for item in output:
            expected = json.loads(subprocess.check_output([str(EXTRACT)], input=panes[item["block_id"]]["text"].encode()))
            self.assertEqual(item["candidates"], expected)

    def test_serial_and_parallel_cpu_settings_match(self):
        panes = {f"pane-{i}": {"delay": 0.02, "text": 'old shared\nnew shared "/tmp/my project"\n'} for i in range(4)}
        serial = self.invoke(panes, jobs=1)
        self.assertEqual(serial.returncode, 0, serial.stderr)
        self.assertEqual(self.peak(), 1)
        parallel = self.invoke(panes, jobs=4, extract_jobs=2)
        self.assertEqual(parallel.returncode, 0, parallel.stderr)
        self.assertEqual(json.loads(serial.stdout), json.loads(parallel.stdout))
        self.assertGreater(self.peak(), 1)

    def test_missing_peer_is_reported_without_losing_other_panes(self):
        process = self.invoke({"origin": {"text": "origin-token"}, "closed": {"error": "Error: block not_found"}})
        self.assertEqual(process.returncode, 0, process.stderr)
        panes = json.loads(process.stdout)["panes"]
        self.assertIn("candidates", panes[0])
        self.assertIn("not_found", panes[1]["error"])
        self.assertNotIn("candidates", panes[1])

    def test_missing_origin_fails_with_no_partial_output(self):
        process = self.invoke({"origin": {"error": "Error: block not found"}})
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(process.stdout, b"")
        self.assertIn(b"origin", process.stderr)

    def test_timeout_terminates_capture(self):
        process = self.invoke({"origin": {"delay": 30, "text": "never-token"}}, capture_timeout_ms=1000)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b"timed out", process.stderr)
        self.assertEqual(process.stdout, b"")
        self.assertTrue(self.events())
        self.assert_children_stopped()

    def test_fatal_peer_cancels_an_inflight_capture(self):
        process = self.invoke({"origin": {"delay": 30, "text": "never-token"},
                               "failed": {"delay": 0.2, "error": "synthetic capture failed"}}, jobs=2)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b"synthetic capture failed", process.stderr)
        self.assertEqual(process.stdout, b"")
        self.assert_children_stopped()

    def test_invalid_utf8_is_rejected(self):
        process = self.invoke({"origin": {"bytes": [255]}})
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b"UTF-8", process.stderr)
        self.assertEqual(process.stdout, b"")

    def test_large_stdout_and_stderr_are_drained(self):
        text = ("plain-token " * 10 + "\n") * 10000 + "final-token\n"
        process = self.invoke({"origin": {"text": text, "stderr": "e" * 1048576}})
        self.assertEqual(process.returncode, 0, process.stderr)
        expected = json.loads(subprocess.check_output([str(EXTRACT)], input=text.encode()))
        self.assertEqual(json.loads(process.stdout)["panes"][0]["candidates"], expected)

    def test_server_argument_is_passed_without_shell_interpretation(self):
        process = self.invoke({"origin": {"text": "exact-token"}}, server="host with spaces")
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_invalid_settings_are_rejected_before_capture(self):
        for options in [{"jobs": 0}, {"jobs": 33}, {"jobs": 1, "extract_jobs": 2},
                        {"capture_timeout_ms": 0}, {"session_id": ""}, {"panes": []},
                        {"options": {"mode": "invalid"}}, {"options": {"max_items": 2001}},
                        {"options": {"max_lines": 0}}]:
            with self.subTest(options=options):
                process = self.invoke({"origin": {"text": "exact-token"}}, **options)
                self.assertNotEqual(process.returncode, 0)
                self.assertEqual(process.stdout, b"")
                self.assertEqual(self.events(), [])


if __name__ == "__main__":
    unittest.main()
