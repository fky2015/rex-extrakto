"""Restore a source pane's zoom when its floating picker closes."""
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import time

from picker_support import wait_for_picker

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = Path(os.getenv("REX_EXTRAKTO_PLUGIN", str(ROOT / "extrakto.lua")))
ARTIFACTS = ROOT / "target/test-results/zoom"
URL = "https://github.com/laktak/extrakto"
LINE = 'Zoom peer Unicode: echo "你好"  /tmp/path with spaces'
REX = shutil.which("rex")


def cli(args, check=True):
    output = subprocess.run([REX] + args, capture_output=True, timeout=30)
    if check and output.returncode:
        raise RuntimeError(output.stderr.decode())
    return output


def result(args):
    return json.loads(cli(args).stdout)


def main():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    fixtures = {"A": "zoom-source-token\n", "B": URL + "\n" + LINE + "\n", "C": "unrelated-tab-token\n"}
    for name, text in fixtures.items():
        (ARTIFACTS / (name + ".txt")).write_text(text)

    def receiver(name):
        return [sys.executable, str(ROOT / "tests/helpers/tab_receiver.py"),
                str(ARTIFACTS / (name + ".bin")), str(ARTIFACTS / (name + ".txt"))]

    created = result(["new", "rex-extrakto-zoom-check", "--window", "[GPT-6] Source",
                      "--shell=none", "--json", "--"] + receiver("A"))
    session = created["session_id"]
    previous = subprocess.check_output(["/usr/bin/pbpaste"])
    owned_clipboard = None
    try:
        origin = created["initial_windows"][0]["block_ids"][0]
        peer = result(["split", "-s", session, "-b", origin, "--focus=false",
                       "--label", "[GPT-6] Peer", "--shell=none", "--json", "--"] + receiver("B"))["block_ids"][0]
        other = result(["run", "-s", session, "--focus=false", "--label", "[GPT-6] Other tab",
                        "--shell=none", "--json", "--"] + receiver("C"))
        for block, text in [(origin, "zoom-source-token"), (peer, URL), (other["block_ids"][0], "unrelated-tab-token")]:
            wait_for_picker(cli, session, block, text)
        zoom = result(["api", "call", "session.zoom_block", json.dumps({
            "session_id": session, "block_id": origin, "zoom": True})])
        assert zoom["zoomed"]
        view = result(["session", "inspect", session, "--json"])
        original_window = view["active_window_id"]
        original_layers = view["windows"][0]["layers"]

        def current():
            return result(["session", "inspect", session, "--json"])

        def preserved(snapshot, zoomed=True, focused=None):
            source = next(w for w in snapshot["windows"] if w["window_id"] == original_window)
            assert source.get("zoomed_block_id") == (origin if zoomed else None), source
            if focused is not False:
                assert source["focused_block_id"] == (focused or origin), source
            assert source["layers"] == original_layers

        base = ["do", str(PLUGIN), "--action", "rex_extrakto", "-s", session, "-b", origin, "--json"]
        preview = result(base + ["scope=tab", "preview=true"])
        assert URL in preview["values"].values()
        preserved(current())
        assert current()["active_window_id"] == original_window

        def picker(scope, query, key, name, mode="word", change_focus=None, zoomed=True):
            result(["api", "call", "session.zoom_block", json.dumps({
                "session_id": session, "block_id": origin, "zoom": zoomed})])
            path = ARTIFACTS / (name + ".json")
            path.unlink(missing_ok=True)
            opened = result(base + ["scope=" + scope, "mode=" + mode, "query=" + query,
                                    "result_file=" + str(path)])
            assert opened["presentation"] == "floating", opened
            block = opened["block_ids"][0]
            wait_for_picker(cli, session, block, "Tab: copy   Enter: insert   Esc: cancel", query)
            during = current()
            source = next(w for w in during["windows"] if w["window_id"] == original_window)
            assert not source.get("zoomed_block_id") and source["focused_block_id"] == block, source
            assert source["layers"][:-1] == original_layers
            assert source["layers"][-1]["layer_id"] == opened["layer_id"]
            assert len(during["windows"]) == 2 and during["active_window_id"] == original_window
            if scope == "session":
                while_open = result(base + ["scope=session", "preview=true"])
                assert len(while_open["panes"]) == 3
            state_path = None
            if key == "close":
                process = result(["block", "call", "-s", session, "-b", block, "process"])
                command = subprocess.check_output(["ps", "-p", str(process["child"]["pid"]), "-o", "args="]).decode()
                state_path = Path(shlex.split(command)[-1])
                assert json.loads(state_path.read_text())["target"]["session_id"] == session
            if change_focus:
                cli(["focus", "-s", session, other["window_id"] if change_focus == "tab" else peer])
            if key == "close":
                cli(["block", "close", "-s", session, block])
            else:
                cli(["send-key", "-s", session, "-b", block, key])
            ended = cli(["wait", "-s", session, block, "--for", "5s", "--json"], check=False)
            if ended.returncode and not path.exists() and key != "close":
                raise RuntimeError(ended.stderr.decode())
            deadline = time.monotonic() + 5
            while not path.is_file() or (state_path and state_path.parent.exists()):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Picker did not finish lifecycle cleanup")
                time.sleep(0.02)
            after = current()
            preserved(after, zoomed=zoomed and not change_focus,
                      focused=False if change_focus == "tab" else peer if change_focus == "pane" else origin)
            assert after["active_window_id"] == (other["window_id"] if change_focus == "tab" else original_window)
            assert len(after["windows"]) == 2
            return json.loads(path.read_text())

        copied = picker("tab", "extko", "tab", "copy")
        owned_clipboard = URL.encode()
        assert copied["status"] == "copied" and copied["text"] == URL
        assert subprocess.check_output(["/usr/bin/pbpaste"]) == owned_clipboard
        assert (ARTIFACTS / "A.bin").read_bytes() == (ARTIFACTS / "B.bin").read_bytes() == b""
        inserted = picker("session", "Zoom peer Unicode", "enter", "insert", mode="line")
        assert inserted["status"] == "inserted" and inserted["text"] == LINE
        deadline = time.monotonic() + 5
        while (ARTIFACTS / "A.bin").read_bytes() != LINE.encode():
            if time.monotonic() >= deadline:
                raise RuntimeError("Source did not receive exact text")
            time.sleep(0.02)
        assert (ARTIFACTS / "B.bin").read_bytes() == (ARTIFACTS / "C.bin").read_bytes() == b""
        assert picker("pane", "zoom-source-token", "escape", "cancel")["status"] == "cancelled"
        assert picker("tab", "extko", "escape", "changed-tab", change_focus="tab")["status"] == "cancelled"
        assert picker("tab", "extko", "escape", "changed-pane", change_focus="pane")["status"] == "cancelled"
        assert picker("tab", "extko", "close", "closed-pane")["status"] == "cancelled"
        assert picker("pane", "zoom-source-token", "close", "closed-unzoomed", zoomed=False)["status"] == "cancelled"
        assert picker("tab", "extko", "close", "closed-changed-tab", change_focus="tab")["status"] == "cancelled"
        assert picker("tab", "extko", "close", "closed-changed-pane", change_focus="pane")["status"] == "cancelled"
        assert (ARTIFACTS / "A.bin").read_bytes() == LINE.encode()
        assert subprocess.check_output(["/usr/bin/pbpaste"]) == owned_clipboard
        print("PASS: floating picker stays in the source tab; copy, insert and cancel restore source zoom", flush=True)
        print("PASS: direct pane close restores focus/zoom, cleans state, and respects intentional focus changes", flush=True)
        (ARTIFACTS / "results.json").write_text(json.dumps({
            "zoom_restored": True, "floating_picker": True, "layout_preserved": True, "copy": True,
            "exact_insert": True, "cancel": True, "return_to_origin": True,
            "changed_focus_respected": True, "floating_layer_removed": True,
            "direct_close": True, "closed_state_cleaned": True,
        }) + "\n")
    finally:
        if owned_clipboard and subprocess.check_output(["/usr/bin/pbpaste"]) == owned_clipboard:
            subprocess.run(["/usr/bin/pbcopy"], input=previous, check=True)
        cli(["kill", session])


if __name__ == "__main__":
    main()
