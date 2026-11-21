"""Check configurable scopes across tabs, with isolated terminal receivers."""
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
ARTIFACTS = ROOT / "target/test-results/session"
LINE = '跨 tab 插入：echo  "你好" --path=/tmp/session test'


def cli(args, check=True):
    result = subprocess.run([REX] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode())
    return result


def result(args):
    return json.loads(cli(args).stdout)


def receiver(name):
    return [PYTHON, str(ROOT / "tests/helpers/tab_receiver.py"),
            str(ARTIFACTS / f"{name}.bin"), str(ARTIFACTS / f"{name}.txt")]


def main():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "results.json").unlink(missing_ok=True)
    fixtures = {
        "A": "shared-session-token\norigin-session-token\n",
        "B": "".join(f"dense-b-{i:05d}\n" for i in range(3500)) + "shared-session-token\nunique-pane-b\n",
        "C": "shared-session-token\n" + LINE + "\nunique-pane-c\n",
        "D": "shared-session-token\nunique-floating-pane\n",
        "E": "excluded-other-session-token\n",
    }
    for name, text in fixtures.items():
        (ARTIFACTS / f"{name}.txt").write_text(text)
    sessions = []
    open_pickers = set()
    previous = subprocess.check_output(["/usr/bin/pbpaste"])
    owned_clipboard = None
    try:
        created = result(["new", "session-scope-check", "--json", "--shell=none", "--"] + receiver("A"))
        session = created["session_id"]
        sessions.append(session)
        view = result(["session", "inspect", session, "--json"])
        window = view["windows"][0]["window_id"]
        origin = view["windows"][0]["layers"][0]["blocks"][0]["block_id"]
        cli(["window", "rename", "-s", session, window, "Shared tab"])
        dense = result(["split", "-s", session, "-b", origin, "--split=right", "--focus=false",
                        "--shell=none", "--label", "Dense pane", "--json", "--"] + receiver("B"))["block_ids"][0]
        other_tab = result(["run", "-s", session, "--focus=false", "--shell=none", "--label", "shell",
                            "--json", "--"] + receiver("C"))
        other_window = other_tab["window_id"]
        other_pane = other_tab["block_ids"][0]
        cli(["window", "rename", "-s", session, other_window, "Shared tab"])
        result(["api", "call", "session.new_layer", json.dumps({
            "session_id": session, "window_id": other_window,
            "bounds": [0.2, 0.2, 0.5, 0.5], "focus": False,
            "layout": {"block": {"flavor": "com.superlogical.terminal.shell", "label": "Floating pane",
                                   "options": {"command": receiver("D"), "shell": "none"}}},
        })])
        external = result(["new", "excluded-session-check", "--json", "--shell=none", "--"] + receiver("E"))
        sessions.append(external["session_id"])
        external_pane = external["initial_windows"][0]["block_ids"][0]
        time.sleep(0.25)
        cli(["focus", "-s", session, other_window])
        base = ["do", str(PLUGIN), "--action", "rex_extrakto",
                "-s", session, "-b", origin, "--json"]

        pane = result(base + ["scope=pane", "preview=true"])
        default = result(base + ["preview=true"])
        tab = result(base + ["scope=tab", "preview=true"])
        full = result(base + ["scope=session", "preview=true"])
        assert result(base + ["scope=session", "preview=true", "jobs=1"]) == full
        assert result(base + ["scope=session", "preview=true", "extract_jobs=2"]) == full
        assert pane["scope"] == default["scope"] == "pane"
        assert "unique-pane-b" not in pane["values"].values()
        assert tab["scope"] == "tab" and len(tab["panes"]) == 2
        assert "unique-pane-b" in tab["values"].values()
        assert "unique-pane-c" not in tab["values"].values()
        assert full["scope"] == "session" and len(full["panes"]) == 4
        assert full["window_id"] == window
        values = list(full["values"].values())
        assert "excluded-other-session-token" not in values
        assert values.count("shared-session-token") == 1
        uniques = {"origin-session-token", "unique-pane-b", "unique-pane-c", "unique-floating-pane"}
        assert uniques <= set(values)
        assert {p["window_id"] for p in full["panes"]} == {window, other_window}
        assert next(p for p in full["panes"] if p["block_id"] == dense)["source_lines"] == 3000
        labels = {item["title"]: item["subtitle"] for item in full["items"]}
        assert labels["origin-session-token"].startswith("1: Shared tab / ")
        assert labels["unique-pane-c"].startswith("2: Shared tab / ")
        assert "+2" in labels["shared-session-token"]
        small = result(base + ["scope=session", "preview=true", "max_items=4"])
        assert set(small["values"].values()) == uniques and small["limited"]
        recent = result(base + ["scope=session", "preview=true", "max_lines=1"])
        assert set(recent["values"].values()) == uniques and recent["source_lines"] == 4
        lines = result(base + ["scope=session", "mode=line", "preview=true"])
        assert LINE in lines["values"].values()
        lines_action = result(["do", str(PLUGIN), "--action", "rex_extrakto_lines",
                               "-s", session, "-b", origin, "scope=session", "preview=true", "--json"])
        assert LINE in lines_action["values"].values()
        override = result(["do", str(PLUGIN), "--action", "rex_extrakto",
                           "-s", external["session_id"], "-b", external_pane,
                           "source_session=" + session, "source_block=" + origin,
                           "scope=session", "preview=true", "--json"])
        assert override["values"] == full["values"]
        invalid = cli(base + ["scope=invalid", "preview=true"], check=False)
        assert invalid.returncode and b"scope" in invalid.stderr
        print("PASS: pane/tab/session boundaries, source overrides, per-pane limits, dedup and fair cap", flush=True)

        # Keep a picker open in the other tab while searching the whole session.
        hold_path = ARTIFACTS / "hold.json"
        hold_path.unlink(missing_ok=True)
        hold = result(["do", str(PLUGIN), "--action", "rex_extrakto",
                       "-s", session, "-b", other_pane, "query=unique-pane-c",
                       "result_file=" + str(hold_path), "--json"])["block_ids"][0]
        open_pickers.add(hold)
        time.sleep(0.25)

        def finish(block, key, path):
            cli(["send-key", "-s", session, "-b", block, key])
            ended = cli(["wait", "-s", session, block, "--for", "5s", "--json"], check=False)
            if ended.returncode and not path.exists():
                raise RuntimeError(ended.stderr.decode())
            open_pickers.discard(block)
            return json.loads(path.read_text())

        def picker(query, key, filename, mode="word"):
            path = ARTIFACTS / filename
            path.unlink(missing_ok=True)
            opened = result(base + ["scope=session", "mode=" + mode, "query=" + query,
                                    "result_file=" + str(path)])
            assert opened["scope"] == "session" and opened["pane_count"] == 4
            block = opened["block_ids"][0]
            open_pickers.add(block)
            time.sleep(0.25)
            view = result(["session", "inspect", session, "--json"])
            containing = [w["window_id"] for w in view["windows"] for layer in w["layers"]
                          for p in layer["blocks"] if p["block_id"] == block]
            assert containing == [window]
            screen = cli(["capture", "-s", session, "-b", block, "--trim", "--unwrap"]).stdout.decode()
            assert "Session extract>" in screen and query in screen, screen
            during = result(base + ["scope=session", "preview=true"])
            assert len(during["panes"]) == 4
            assert not open_pickers.intersection(p["block_id"] for p in during["panes"])
            return finish(block, key, path)

        copied = picker("unique-pane-b", "tab", "copy.json")
        owned_clipboard = b"unique-pane-b"
        assert copied["status"] == "copied" and copied["text"] == "unique-pane-b"
        assert subprocess.check_output(["/usr/bin/pbpaste"]) == owned_clipboard
        assert all((ARTIFACTS / f"{name}.bin").read_bytes() == b"" for name in fixtures)
        inserted = picker("跨 tab 插入", "enter", "insert.json", mode="line")
        cli(["block", "call", "-s", session, "-b", origin, "process"])
        assert inserted["status"] == "inserted" and inserted["text"] == LINE
        assert (ARTIFACTS / "A.bin").read_bytes() == LINE.encode()
        assert all((ARTIFACTS / f"{name}.bin").read_bytes() == b"" for name in ["B", "C", "D", "E"])
        assert subprocess.check_output(["/usr/bin/pbpaste"]) == owned_clipboard
        cancelled = picker("unique-pane-c", "escape", "cancel.json")
        assert cancelled["status"] == "cancelled"
        assert finish(hold, "escape", hold_path)["status"] == "cancelled"
        assert (ARTIFACTS / "A.bin").read_bytes() == LINE.encode()
        assert subprocess.check_output(["/usr/bin/pbpaste"]) == owned_clipboard
        print("PASS: pickers across tabs are excluded; popup stays in origin tab; exact copy/insert/cancel", flush=True)
        (ARTIFACTS / "results.json").write_text(json.dumps({
            "scopes": ["pane", "tab", "session"], "dedup": True, "fair_cap": True,
            "source_override": True, "other_session_excluded": True, "picker_excluded": True,
            "popup_origin_tab": True, "copy": True, "insert_origin": True, "cancel": True,
        }) + "\n")
    finally:
        for block in open_pickers:
            cli(["send-key", "-s", sessions[0], "-b", block, "escape"], check=False)
            cli(["wait", "-s", sessions[0], block, "--for", "2s", "--json"], check=False)
        if owned_clipboard and subprocess.check_output(["/usr/bin/pbpaste"]) == owned_clipboard:
            subprocess.run(["/usr/bin/pbcopy"], input=previous, check=True)
        for session in reversed(sessions):
            cli(["kill", session], check=False)
        print("Session test sessions removed", flush=True)


if __name__ == "__main__":
    main()
