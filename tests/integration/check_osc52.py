"""Verify Rex receives an exact OSC 52 clipboard write from the real picker."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = Path(os.getenv("REX_EXTRAKTO_PLUGIN", str(ROOT / "extrakto.lua")))
REX = shutil.which("rex")
ARTIFACTS = ROOT / "target/test-results/osc52"
ARTIFACTS.mkdir(parents=True, exist_ok=True)
TEXT = "OSC52 中文候选 /tmp/path with spaces"
(ARTIFACTS / "fixture.txt").write_text(TEXT + "\n")
previous = subprocess.check_output(["/usr/bin/pbpaste"]) if sys.platform == "darwin" else None


def cli(args, check=True):
    result = subprocess.run([REX] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode())
    return result


def result(args):
    return json.loads(cli(args).stdout)


created = result(["new", "osc52-copy-check", "--json", "--shell=none", "--", sys.executable,
                  str(ROOT / "tests/helpers/tab_receiver.py"), str(ARTIFACTS / "input.bin"),
                  str(ARTIFACTS / "fixture.txt")])
session = created["session_id"]
try:
    origin = created["initial_windows"][0]["block_ids"][0]
    deadline = time.monotonic() + 5
    while TEXT not in cli(["capture", "-s", session, "-b", origin, "--trim", "--unwrap"]).stdout.decode():
        if time.monotonic() >= deadline:
            raise RuntimeError("OSC52 fixture did not become ready")
        time.sleep(0.02)
    path = ARTIFACTS / "copy.json"
    path.unlink(missing_ok=True)
    opened = result(["do", str(PLUGIN), "--action", "rex_extrakto_lines",
                     "-s", session, "-b", origin, "clipboard=osc52", "keep_open=true",
                     "query=OSC52", "result_file=" + str(path), "--json"])
    block = opened["block_ids"][0]
    cli(["block", "call", "-s", session, "-b", block, "process"])
    time.sleep(0.25)
    cli(["send-key", "-s", session, "-b", block, "tab"])
    cli(["wait", "-s", session, block, "--for", "5s", "--json"])
    copied = json.loads(path.read_text())
    assert copied["status"] == "copied" and copied["text"] == TEXT
    clipboard = result(["block", "call", "-s", session, "-b", block, "clipboard_read"])
    assert clipboard["data"] == TEXT and clipboard["eof"]
    assert clipboard["total"] == len(TEXT.encode())
    assert (ARTIFACTS / "input.bin").read_bytes() == b""
    (ARTIFACTS / "results.json").write_text(json.dumps({"osc52": True, "exact_utf8": True}) + "\n")
    print("PASS: Rex receives exact OSC52 text from Tab; source pane receives no input", flush=True)
finally:
    cli(["kill", session])
    if previous is not None and subprocess.check_output(["/usr/bin/pbpaste"]) == TEXT.encode():
        subprocess.run(["/usr/bin/pbcopy"], input=previous, check=True)
