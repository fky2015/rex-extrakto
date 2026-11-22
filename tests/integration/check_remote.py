"""Verify a local Lua script forwards to a remote Rust picker.

Use a disposable Rex server and an empty remote artifact directory. --bootstrap
also checks first-use installation from the current public release.
"""
import argparse
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import time

from picker_support import wait_for_picker

ROOT = Path(__file__).resolve().parents[2]
TEXT = 'Remote Unicode: echo "你好"  /tmp/path with spaces'
URL = "https://github.com/laktak/extrakto"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, help="Rex SSH endpoint, including the test socket")
    parser.add_argument("--ssh", required=True, help="SSH user@host for reading test artifacts")
    parser.add_argument("--remote-dir", required=True, help="Reserved directory for test artifacts")
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--zoom", action="store_true", help="Keep the source pane zoomed during searches")
    args = parser.parse_args()
    rex = shutil.which("rex")

    def cli(argv, check=True):
        result = subprocess.run([rex, "-S", args.server] + argv, capture_output=True, timeout=90)
        if check and result.returncode:
            raise RuntimeError(result.stderr.decode())
        return result

    def result(argv):
        return json.loads(cli(argv).stdout)

    def read(name):
        path = args.remote_dir + "/" + name
        return subprocess.check_output(["ssh", "-o", "BatchMode=yes", args.ssh,
                                        "cat -- " + shlex.quote(path)], timeout=20)

    def outcome(name):
        return json.loads(read(name))

    receiver = (ROOT / "tests/helpers/tab_receiver.py").read_text()
    fixture = (URL + "\n" + TEXT + "\n").encode()

    def command(name):
        prefix = "from pathlib import Path\n"
        prefix += "Path(" + repr(args.remote_dir) + ").mkdir(parents=True, exist_ok=True)\n"
        prefix += "Path(" + repr(args.remote_dir + "/" + name + ".txt") + ").write_bytes(" + repr(fixture) + ")\n"
        return ["python3", "-c", prefix + receiver,
                args.remote_dir + "/" + name + ".bin", args.remote_dir + "/" + name + ".txt"]

    created = result(["new", "rex-extrakto-remote-check", "--window", "[GPT-6] Remote source",
                      "--shell=none", "--json", "--"] + command("A"))
    session = created["session_id"]
    try:
        origin = created["initial_windows"][0]["block_ids"][0]
        peer = result(["split", "-s", session, "-b", origin, "--focus=false",
                       "--label", "[GPT-6] Remote peer", "--shell=none", "--json", "--"] + command("B"))["block_ids"][0]
        for block in (origin, peer):
            wait_for_picker(cli, session, block, URL, TEXT)
        original_window = result(["session", "inspect", session, "--json"])["active_window_id"]
        if args.zoom:
            result(["api", "call", "session.zoom_block", json.dumps({
                "session_id": session, "block_id": origin, "zoom": True})])

        def check_zoom(returned=False):
            if args.zoom:
                view = result(["session", "inspect", session, "--json"])
                source = next(w for w in view["windows"] if w["window_id"] == original_window)
                assert source["zoomed_block_id"] == origin and source["focused_block_id"] == origin, source
                if returned:
                    assert view["active_window_id"] == original_window, view

        base = ["do", str(ROOT / "extrakto.lua"), "--action", "rex_extrakto",
                "-s", session, "-b", origin, "scope=tab", "--json"]

        if args.bootstrap:
            missing = cli(base + ["preview=true"], check=False)
            assert missing.returncode and b"Install rex-extrakto" in missing.stderr
            opened = result(base + ["query=extko", "keep_open=true",
                                    "result_file=" + args.remote_dir + "/bootstrap.json"])
            assert opened["status"] == "installing", opened
            installation = opened["block_ids"][0]
            ended = cli(["wait", "-s", session, installation, "--for", "60s", "--json"], check=False)
            if ended.returncode:
                screen = cli(["capture", "-s", session, "-b", installation, "--trim", "--unwrap"], check=False)
                raise RuntimeError("First-use installation failed: " + screen.stdout.decode() + ended.stderr.decode())
            view = result(["session", "inspect", session, "--json"])
            pickers = [b["block_id"] for w in view["windows"] for layer in w["layers"]
                       for b in layer["blocks"] if b.get("label") == "Extract text"]
            assert len(pickers) == 1, view
            block = pickers[0]
            wait_for_picker(cli, session, block, "Tab extract>", "extko")
            check_zoom()
            cli(["send-key", "-s", session, "-b", block, "escape"])
            cli(["wait", "-s", session, block, "--for", "5s", "--json"])
            assert outcome("bootstrap.json")["status"] == "cancelled"
            cli(["block", "close", "-s", session, block])
            print("PASS: missing runtime installs on the target and resumes the original search", flush=True)

        runtime = result(["do", "rex_extrakto_runtime", "--json"])
        assert runtime["picker"] == "rust" and not runtime["directory"].startswith("/Users/")
        preview = result(base + ["preview=true"])
        assert preview["scope"] == "tab" and len(preview["panes"]) == 2
        assert URL in preview["values"].values()
        assert list(preview["values"].values()).count(URL) == 1

        def picker(query, key, name, mode="word"):
            opened = result(base + ["mode=" + mode, "query=" + query, "keep_open=true",
                                    "result_file=" + args.remote_dir + "/" + name + ".json"])
            assert opened["status"] == "popup" and opened["pane_count"] == 2, opened
            assert opened["presentation"] == ("tab" if args.zoom else "floating"), opened
            block = opened["block_ids"][0]
            wait_for_picker(cli, session, block, "Tab extract>", query)
            check_zoom()
            cli(["send-key", "-s", session, "-b", block, key])
            cli(["wait", "-s", session, block, "--for", "5s", "--json"])
            data = outcome(name + ".json")
            if key == "tab":
                clipboard = result(["block", "call", "-s", session, "-b", block, "clipboard_read"])
                assert clipboard["data"] == URL and clipboard["eof"]
            cli(["block", "close", "-s", session, block])
            return data

        copied = picker("extko", "tab", "copy")
        assert copied["status"] == "copied" and copied["text"] == URL
        assert read("A.bin") == read("B.bin") == b""
        inserted = picker("Remote Unicode", "enter", "insert", mode="line")
        assert inserted["status"] == "inserted" and inserted["text"] == TEXT
        deadline = time.monotonic() + 5
        while read("A.bin") != TEXT.encode():
            if time.monotonic() >= deadline:
                raise RuntimeError("Remote source did not receive exact text")
            time.sleep(0.02)
        assert read("B.bin") == b""
        assert picker("extko", "escape", "cancel")["status"] == "cancelled"
        assert read("A.bin") == TEXT.encode() and read("B.bin") == b""
        if args.zoom:
            opened = result(base + ["query=extko", "result_file=" + args.remote_dir + "/return.json"])
            block = opened["block_ids"][0]
            wait_for_picker(cli, session, block, "Tab extract>", "extko")
            check_zoom()
            cli(["send-key", "-s", session, "-b", block, "escape"])
            cli(["wait", "-s", session, block, "--for", "5s", "--json"], check=False)
            assert outcome("return.json")["status"] == "cancelled"
            check_zoom(returned=True)
            print("PASS: remote source remains zoomed and the temporary picker returns to its tab", flush=True)
        print("PASS: local script routes to remote Rust/fzf; tab capture, fuzzy copy, exact insert and cancel", flush=True)
    finally:
        cli(["kill", session])
        print("Disposable remote test session removed", flush=True)


if __name__ == "__main__":
    main()
