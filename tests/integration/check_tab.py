"""Test tab scope with four source panes, another tab and an open picker."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from picker_support import wait_for_picker

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = Path(os.getenv("REX_EXTRAKTO_PLUGIN", str(ROOT / "extrakto.lua")))
REX = shutil.which("rex")
PYTHON = sys.executable
ARTIFACTS = ROOT / "target/test-results/tab"
ARTIFACTS.mkdir(parents=True, exist_ok=True)
fixtures = {
    "A": "shared-tab-token\norigin-tab-token\n",
    "B": "".join(f"dense-b-{i:05d}\n" for i in range(3500)) + "shared-tab-token\nunique-pane-b\n",
    "C": "shared-tab-token\nunique-pane-c\n",
    "E": "shared-tab-token\nunique-floating-pane\n",
    "D": "excluded-other-tab-token\n",
}
for name, text in fixtures.items():
    (ARTIFACTS / f"{name}.txt").write_text(text)


def cli(args, check=True):
    result = subprocess.run([REX] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode())
    return result


def result(args):
    return json.loads(cli(args).stdout)


def receiver(name):
    return [PYTHON, str(ROOT / "tests/helpers/tab_receiver.py"), str(ARTIFACTS / f"{name}.bin"), str(ARTIFACTS / f"{name}.txt")]


created = result(["new", "tab-scope-check", "--json", "--shell=none", "--"] + receiver("A"))
session = created["session_id"]
previous = subprocess.check_output(["/usr/bin/pbpaste"])
owned_clipboard = None
try:
    view = result(["session", "inspect", session, "--json"])
    window = view["windows"][0]["window_id"]
    origin = view["windows"][0]["layers"][0]["blocks"][0]["block_id"]
    result(["split", "-s", session, "-b", origin, "--split=right", "--focus=false", "--shell=none",
            "--label", "Pane B", "--json", "--"] + receiver("B"))
    # A regular pane with the picker label must still be included.
    result(["split", "-s", session, "-b", origin, "--split=below", "--focus=false", "--shell=none",
            "--label", "Extract text", "--json", "--"] + receiver("C"))
    floating = {"session_id": session, "window_id": window, "bounds": [0.2, 0.2, 0.5, 0.5], "focus": False,
                "layout": {"block": {"flavor": "com.superlogical.terminal.shell", "label": "Floating pane",
                                      "options": {"command": receiver("E"), "shell": "none"}}}}
    result(["api", "call", "session.new_layer", json.dumps(floating)])
    other = result(["run", "-s", session, "--label", "Other tab", "--focus=false", "--shell=none", "--json", "--"] + receiver("D"))
    time.sleep(0.25)
    # Resolve scope from the invoking pane even if the session's active tab changed.
    cli(["focus", "-s", session, other["window_id"]])
    base = ["do", str(PLUGIN), "--action", "rex_extrakto_tab",
            "-s", session, "-b", origin, "--json"]
    preview = result(base + ["preview=true"])
    assert result(base + ["preview=true", "jobs=1"]) == preview
    titles = [item["title"] for item in preview["items"]]
    for token in ["origin-tab-token", "unique-pane-b", "unique-pane-c", "unique-floating-pane"]:
        assert token in titles, token
    assert "excluded-other-tab-token" not in titles
    assert titles.count("shared-tab-token") == 1
    assert len(preview["panes"]) == 4
    assert preview["window_id"] == window
    shared = next(item for item in preview["items"] if item["title"] == "shared-tab-token")
    assert "+2" in shared["subtitle"], shared
    small = result(base + ["preview=true", "max_items=4"])
    assert {item["title"] for item in small["items"]} == {
        "origin-tab-token", "unique-pane-b", "unique-pane-c", "unique-floating-pane"}
    assert small["limited"]
    single = result(["do", str(PLUGIN), "--action", "rex_extrakto",
                     "-s", session, "-b", origin, "scope=pane", "preview=true", "--json"])
    assert "unique-pane-b" not in single["values"].values()
    print("PASS: all source-tab panes, per-pane history, global dedup, fair cap and other-tab exclusion", flush=True)

    def picker(query, key, filename, inspect_open=False):
        path = ARTIFACTS / filename
        path.unlink(missing_ok=True)
        opened = result(base + ["query=" + query, "result_file=" + str(path)])
        assert opened["pane_count"] == 4
        block = opened["block_ids"][0]
        cli(["block", "call", "-s", session, "-b", block, "process"])
        wait_for_picker(cli, session, block, "Tab extract>", query)
        if inspect_open:
            during = result(base + ["preview=true"])
            assert len(during["panes"]) == 4
            assert block not in [pane["block_id"] for pane in during["panes"]]
        cli(["send-key", "-s", session, "-b", block, key])
        ended = cli(["wait", "-s", session, block, "--for", "5s", "--json"], check=False)
        if ended.returncode and not path.exists():
            raise RuntimeError(ended.stderr.decode())
        return json.loads(path.read_text())

    copied = picker("unique-pane-b", "tab", "copy.json", inspect_open=True)
    owned_clipboard = b"unique-pane-b"
    assert copied["status"] == "copied" and copied["text"] == "unique-pane-b"
    assert subprocess.check_output(["/usr/bin/pbpaste"]) == owned_clipboard
    assert all((ARTIFACTS / f"{name}.bin").read_bytes() == b"" for name in fixtures)
    inserted = picker("unique-pane-c", "enter", "insert.json")
    cli(["block", "call", "-s", session, "-b", origin, "process"])
    assert inserted["status"] == "inserted" and inserted["text"] == "unique-pane-c"
    assert (ARTIFACTS / "A.bin").read_bytes() == b"unique-pane-c"
    assert all((ARTIFACTS / f"{name}.bin").read_bytes() == b"" for name in ["B", "C", "D", "E"])
    assert subprocess.check_output(["/usr/bin/pbpaste"]) == owned_clipboard
    cancelled = picker("unique-pane-b", "escape", "cancel.json")
    assert cancelled["status"] == "cancelled"
    assert (ARTIFACTS / "A.bin").read_bytes() == b"unique-pane-c"
    print("PASS: copy strips source decoration; Enter inserts into invoking pane; Escape cancels", flush=True)
    (ARTIFACTS / "results.json").write_text(json.dumps({"scope": True, "dedup": True, "fair_cap": True,
                                                      "copy": True, "insert_origin": True, "cancel": True}) + "\n")
finally:
    if owned_clipboard and subprocess.check_output(["/usr/bin/pbpaste"]) == owned_clipboard:
        subprocess.run(["/usr/bin/pbcopy"], input=previous, check=True)
    cli(["kill", session])
    print("Tab test session removed", flush=True)
