"""Exercise the project Lua actions in a disposable Rex session."""
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
PYTHON = sys.executable
ARTIFACTS = ROOT / "target/test-results/picker"
ARTIFACTS.mkdir(parents=True, exist_ok=True)


def cli(args, check=True):
    result = subprocess.run([REX] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode())
    return result


def result(args):
    return json.loads(cli(args).stdout)


session = result(["new", "rex-extrakto-picker-check", "--json", "--shell=none", "--", "/bin/sh"])["session_id"]
previous = subprocess.check_output(["/usr/bin/pbpaste"])
owned_clipboard = None
input_path = ARTIFACTS / "inserted-bytes.bin"
try:
    demo = result(["run", "-s", session, "--keep-open", "--focus=false", "--shell=none", "--json", "--wait",
                   "--", PYTHON, str(ROOT / "tests/helpers/demo.py")])["block_ids"][0]
    preview = result(["do", str(PLUGIN), "--action", "rex_extrakto",
                      "-s", session, "-b", demo, "preview=true", "--json"])
    assert "https://example.com/rex/demo?query=hello&mode=word" in preview["values"].values()
    subprocess.run([PYTHON, str(ROOT / "tests/integration/check_clipboard.py"), session, demo], cwd=ROOT, check=True)
    receiver = result(["run", "-s", session, "--focus=false", "--shell=none", "--json",
                       "--", PYTHON, str(ROOT / "tests/helpers/receiver.py"), str(input_path)])["block_ids"][0]
    # Creation returns before the receiver has printed its fixture.
    deadline = time.monotonic() + 5
    while True:
        screen = cli(["capture", "-s", session, "-b", receiver, "--trim", "--unwrap"]).stdout.decode()
        if "rex-insert-test-20261120" in screen and 'echo "你好" --target=/tmp/example' in screen:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Receiver fixture did not become ready")
        time.sleep(0.02)

    def picker(action, query, key, filename):
        path = ARTIFACTS / filename
        path.unlink(missing_ok=True)
        opened = result(["do", str(PLUGIN), "--action", action,
                         "-s", session, "-b", receiver, "query=" + query,
                         "result_file=" + str(path), "--json"])
        block = opened["block_ids"][0]
        cli(["block", "call", "-s", session, "-b", block, "process"])
        # Terminal creation returns before Python/fzf has finished drawing its UI.
        time.sleep(0.25)
        screen = cli(["capture", "-s", session, "-b", block, "--trim", "--unwrap"]).stdout.decode()
        assert "Tab: copy   Enter: insert   Esc: cancel" in screen, screen
        cli(["send-key", "-s", session, "-b", block, key])
        ended = cli(["wait", "-s", session, block, "--for", "5s", "--json"], check=False)
        if ended.returncode and not path.exists():
            raise RuntimeError(ended.stderr.decode())
        return json.loads(path.read_text())

    copied = picker("rex_extrakto", "rex-insert-test", "tab", "copy.json")
    owned_clipboard = b"rex-insert-test-20261120"
    assert copied["status"] == "copied"
    assert subprocess.check_output(["/usr/bin/pbpaste"]) == owned_clipboard
    assert input_path.read_bytes() == b""
    print("PASS: Rust action + Tab copies, source pane receives nothing", flush=True)

    inserted = picker("rex_extrakto_lines", "你好", "enter", "insert.json")
    cli(["block", "call", "-s", session, "-b", receiver, "process"])
    expected = 'echo "你好" --target=/tmp/example'.encode()
    assert inserted["status"] == "inserted"
    assert input_path.read_bytes() == expected
    assert subprocess.check_output(["/usr/bin/pbpaste"]) == owned_clipboard
    print("PASS: Enter pastes exact UTF-8 text into the original pane, without CR/LF", flush=True)

    cancelled = picker("rex_extrakto", "rex-insert-test", "escape", "cancel.json")
    assert cancelled["status"] == "cancelled"
    assert input_path.read_bytes() == expected
    assert subprocess.check_output(["/usr/bin/pbpaste"]) == owned_clipboard
    print("PASS: Escape changes neither source pane nor clipboard", flush=True)
    (ARTIFACTS / "results.json").write_text(json.dumps({"copy": True, "insert": True, "cancel": True}) + "\n")
finally:
    if owned_clipboard and subprocess.check_output(["/usr/bin/pbpaste"]) == owned_clipboard:
        subprocess.run(["/usr/bin/pbcopy"], input=previous, check=True)
    cli(["kill", session])
    print("Disposable test session removed", flush=True)
