"""Keep a source pane zoomed while searching, copying and inserting."""
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

        def preserved(snapshot):
            source = next(w for w in snapshot["windows"] if w["window_id"] == original_window)
            assert source["zoomed_block_id"] == origin, source
            assert source["focused_block_id"] == origin, source
            assert source["layers"] == original_layers

        base = ["do", str(PLUGIN), "--action", "rex_extrakto", "-s", session, "-b", origin, "--json"]
        preview = result(base + ["scope=tab", "preview=true"])
        assert URL in preview["values"].values()
        preserved(current())
        assert current()["active_window_id"] == original_window

        def picker(scope, query, key, name, mode="word", change_focus=False):
            path = ARTIFACTS / (name + ".json")
            path.unlink(missing_ok=True)
            opened = result(base + ["scope=" + scope, "mode=" + mode, "query=" + query,
                                    "result_file=" + str(path)])
            assert opened["presentation"] == "tab", opened
            block = opened["block_ids"][0]
            wait_for_picker(cli, session, block, "Tab: copy   Enter: insert   Esc: cancel", query)
            during = current()
            preserved(during)
            assert during["active_window_id"] == opened["window_id"] != original_window
            if scope == "session":
                while_open = result(base + ["scope=session", "preview=true"])
                assert len(while_open["panes"]) == 3
            if change_focus:
                cli(["focus", "-s", session, other["window_id"]])
            cli(["send-key", "-s", session, "-b", block, key])
            ended = cli(["wait", "-s", session, block, "--for", "5s", "--json"], check=False)
            if ended.returncode and not path.exists():
                raise RuntimeError(ended.stderr.decode())
            after = current()
            preserved(after)
            assert after["active_window_id"] == (other["window_id"] if change_focus else original_window)
            assert opened["window_id"] not in {w["window_id"] for w in after["windows"]}
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
        assert picker("tab", "extko", "escape", "changed-focus", change_focus=True)["status"] == "cancelled"
        assert (ARTIFACTS / "A.bin").read_bytes() == LINE.encode()
        assert subprocess.check_output(["/usr/bin/pbpaste"]) == owned_clipboard
        print("PASS: source zoom/layout survives tab/session/pane search; copy, insert and cancel return to origin", flush=True)
        print("PASS: temporary picker closes and respects an intentional switch to another tab", flush=True)
        (ARTIFACTS / "results.json").write_text(json.dumps({
            "zoom_preserved": True, "layout_preserved": True, "copy": True,
            "exact_insert": True, "cancel": True, "return_to_origin": True,
            "changed_focus_respected": True, "temporary_tab_removed": True,
        }) + "\n")
    finally:
        if owned_clipboard and subprocess.check_output(["/usr/bin/pbpaste"]) == owned_clipboard:
            subprocess.run(["/usr/bin/pbcopy"], input=previous, check=True)
        cli(["kill", session])


if __name__ == "__main__":
    main()
