"""Run against the dedicated demo pane; restore the original Mac clipboard."""
import argparse
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("session")
    parser.add_argument("block")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    plugin = Path(os.getenv("REX_EXTRAKTO_PLUGIN", str(root / "extrakto.lua")))
    base = ["rex", "do", str(plugin), "--action", "rex_extrakto",
            "-s", args.session, "-b", args.block, "--json"]
    preview = json.loads(subprocess.check_output(base + ["preview=true"], text=True))
    values = list(preview["values"].values())
    expected = ["https://example.com/rex/demo?query=hello&mode=word",
                "/tmp/rex demo/project/report.md",
                "终端文本提取",
                "https://example.com/" + "long-path/" * 18 + "result?ok=1"]
    for value in expected:
        assert value in values, f"Missing capture candidate: {value}"
    print("PASS: capture preserves URL queries, quoted paths, Unicode and soft-wrapped lines")
    previous = subprocess.check_output(["/usr/bin/pbpaste"])
    owned_clipboard = None
    try:
        for value in expected:
            copied = json.loads(subprocess.check_output(base + ["select=" + value], text=True))
            owned_clipboard = value.encode()
            assert copied["status"] == "copied"
            assert subprocess.check_output(["/usr/bin/pbpaste"]).decode() == value
        line = "中文候选：终端文本提取 可以复制完整一行"
        subprocess.check_output(base + ["mode=line", "select=" + line], text=True)
        owned_clipboard = line.encode()
        assert subprocess.check_output(["/usr/bin/pbpaste"]).decode() == line
        rejected = subprocess.run(base + ["select=not-in-the-pane"], capture_output=True, text=True)
        assert rejected.returncode != 0
        assert subprocess.check_output(["/usr/bin/pbpaste"]).decode() == line
        print("PASS: exact local clipboard bytes for 4 tokens and a complete line")
        print("PASS: an invalid selection fails without changing the clipboard")
    finally:
        if owned_clipboard is not None and subprocess.check_output(["/usr/bin/pbpaste"]) == owned_clipboard:
            subprocess.run(["/usr/bin/pbcopy"], input=previous, check=True)
            print("Original clipboard restored")


if __name__ == "__main__":
    main()
