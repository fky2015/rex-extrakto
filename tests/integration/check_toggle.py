"""A repeated extraction action cancels the existing picker in that tab."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from picker_support import wait_for_picker, wait_for_block_removal

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = Path(os.getenv("REX_EXTRAKTO_PLUGIN", str(ROOT / "extrakto.lua")))
ARTIFACTS = ROOT / "target/test-results/toggle"
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
    for name in ("A", "B", "C"):
        (ARTIFACTS / (name + ".txt")).write_text("toggle-source-" + name + "\nhttps://github.com/laktak/extrakto\n")

    def receiver(name):
        return [sys.executable, str(ROOT / "tests/helpers/tab_receiver.py"),
                str(ARTIFACTS / (name + ".bin")), str(ARTIFACTS / (name + ".txt"))]

    created = result(["new", "rex-extrakto-toggle-check", "--window", "[GPT-6] Source",
                      "--shell=none", "--json", "--"] + receiver("A"))
    session = created["session_id"]
    clipboard = subprocess.check_output(["/usr/bin/pbpaste"])
    try:
        origin = created["initial_windows"][0]["block_ids"][0]
        result(["split", "-s", session, "-b", origin, "--focus=false", "--label", "[GPT-6] Peer",
                "--shell=none", "--json", "--"] + receiver("B"))
        other = result(["run", "-s", session, "--focus=false", "--label", "[GPT-6] Other tab",
                        "--shell=none", "--json", "--"] + receiver("C"))
        wait_for_picker(cli, session, origin, "toggle-source-A")
        wait_for_picker(cli, session, other["block_ids"][0], "toggle-source-C")
        source_window = result(["session", "inspect", session, "--json"])["active_window_id"]

        def action(block, scope="tab", name="rex_extrakto", **args):
            return result(["do", str(PLUGIN), "--action", name, "-s", session, "-b", block,
                           "--args", json.dumps({"scope": scope, **args}), "--json"])

        held_path = ARTIFACTS / "other.json"
        held_path.unlink(missing_ok=True)
        held = action(other["block_ids"][0], query="extko", result_file=str(held_path))["block_ids"][0]
        wait_for_picker(cli, session, held, "Tab: copy", "extko")
        cases = [("tab", "rex_extrakto", True, True),
                 ("pane", "rex_extrakto", False, False),
                 ("session", "rex_extrakto_lines", False, True)]
        for index, (scope, name, zoomed, ready) in enumerate(cases):
            result(["api", "call", "session.zoom_block", json.dumps({
                "session_id": session, "block_id": origin, "zoom": zoomed})])
            path = ARTIFACTS / (str(index) + ".json")
            path.unlink(missing_ok=True)
            opened = action(origin, scope, name, query="extko", result_file=str(path))
            assert opened["status"] == "popup", opened
            picker = opened["block_ids"][0]
            if ready:
                wait_for_picker(cli, session, picker, "Tab: copy", "extko")
                action(origin, scope, name, preview=True)
            second = action(picker if ready else origin, scope, name)
            assert second["status"] == "cancelled" and second["block_id"] == picker, second
            cli(["wait", "-s", session, picker, "--for", "5s", "--json"], check=False)
            wait_for_block_removal(cli, session, picker)
            assert json.loads(path.read_text())["status"] == "cancelled"
            view = result(["session", "inspect", session, "--json"])
            window = next(w for w in view["windows"] if w["window_id"] == source_window)
            assert window["focused_block_id"] == origin
            assert window.get("zoomed_block_id") == (origin if zoomed else None)
            blocks = {b["block_id"] for w in view["windows"] for layer in w["layers"] for b in layer["blocks"]}
            assert picker not in blocks and held in blocks
            assert all((ARTIFACTS / (name + ".bin")).read_bytes() == b"" for name in ("A", "B", "C"))
            assert subprocess.check_output(["/usr/bin/pbpaste"]) == clipboard

        # A completed diagnostic picker is also dismissed by the shortcut.
        debug = action(origin, query="extko", keep_open=True)["block_ids"][0]
        wait_for_picker(cli, session, debug, "Tab: copy", "extko")
        cli(["send-key", "-s", session, "-b", debug, "escape"])
        cli(["wait", "-s", session, debug, "--for", "5s", "--json"])
        assert action(debug)["status"] == "cancelled"
        cli(["send-key", "-s", session, "-b", held, "escape"])
        cli(["wait", "-s", session, held, "--for", "5s", "--json"], check=False)
        # Capture, placement, and cancellation agree on a cross-tab source override.
        for scope in ("pane", "tab", "session"):
            path = ARTIFACTS / ("override-" + scope + ".json")
            path.unlink(missing_ok=True)
            opened = action(origin, scope, source_block=other["block_ids"][0],
                            query="toggle-source-C", result_file=str(path))
            picker = opened["block_ids"][0]
            wait_for_picker(cli, session, picker, "Tab: copy", "toggle-source-C")
            snapshot = result(["session", "inspect", session, "--json"])
            window = next(w for w in snapshot["windows"] if w["window_id"] == other["window_id"])
            assert window["focused_block_id"] == picker
            second = action(origin, scope, source_block=other["block_ids"][0])
            assert second["status"] == "cancelled" and second["block_id"] == picker, second
            cli(["wait", "-s", session, picker, "--for", "5s", "--json"], check=False)
            wait_for_block_removal(cli, session, picker)
            assert json.loads(path.read_text())["status"] == "cancelled"
        print("PASS: shortcut toggles pane/tab/session pickers, including rapid repeats and independent Lua loads", flush=True)
        print("PASS: focus/zoom restored; clipboard/input unchanged; other-tab picker and previews preserved", flush=True)
        (ARTIFACTS / "results.json").write_text(json.dumps({"toggle": True, "rapid_repeat": True,
            "scopes": ["pane", "tab", "session"], "zoom": True, "focus": True,
            "other_tab_preserved": True, "preview_preserved": True, "debug_picker_closed": True,
            "cross_tab_source_override": True}) + "\n")
    finally:
        cli(["kill", session])


if __name__ == "__main__":
    main()
