#!/usr/bin/env python3
"""Rex's two-action fzf picker: Tab copies; Enter pastes into the original pane."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys


def copy_text(value, backend="auto"):
    if backend == "auto":
        backend = "pbcopy" if sys.platform == "darwin" else "osc52"
    if backend == "pbcopy":
        subprocess.run(["/usr/bin/pbcopy"], input=value.encode(), check=True)
    elif backend == "osc52":
        # Rex forwards OSC 52 clipboard writes to the connected client.
        sys.stdout.buffer.write(b"\x1b]52;c;" + base64.b64encode(value.encode()) + b"\x07")
        sys.stdout.buffer.flush()
    else:
        raise ValueError("clipboard must be auto, pbcopy, or osc52")


def main():
    state_path = Path(sys.argv[1])
    # A terminal-owned identity survives fresh Lua loads and config reloads.
    sys.stdout.write("\x1b]2;rex-extrakto\x07")
    sys.stdout.flush()
    try:
        state = json.loads(state_path.read_text())
        records = []
        def display_text(value):
            return value.replace("\t", " ⇥ ").replace("\n", " ⏎ ").replace("\r", " ⏎ ").replace("\x00", "␀")
        for item in state["items"]:
            display = display_text(item["title"])
            if item.get("subtitle"):
                display += "\t[" + display_text(item["subtitle"]) + "]"
            records.append(f'{item["id"]}\t{display}')
        env = os.environ.copy()
        env.pop("FZF_DEFAULT_OPTS", None)
        env.pop("FZF_DEFAULT_OPTS_FILE", None)
        prompt = {"tab": "Tab extract> ", "session": "Session extract> "}.get(state.get("scope"), "Extract> ")
        selected = subprocess.run(
            [state["fzf"], "--read0", "--print0", "--layout=reverse", "--border=rounded",
             "--no-multi", "--delimiter=\t", "--with-nth=2..", "--nth=1",
             "--expect=tab,enter", "--prompt=" + prompt,
             "--header=Tab: copy   Enter: insert   Esc: cancel",
             "--query=" + state.get("query", "")],
            input=("\0".join(records) + "\0").encode(), stdout=subprocess.PIPE, env=env,
        )
        if selected.returncode in (1, 130):
            result = {"status": "cancelled"}
        elif selected.returncode != 0:
            raise RuntimeError(f"fzf exited with {selected.returncode}")
        else:
            key, record = selected.stdout.split(b"\0", 2)[:2]
            item_id = record.split(b"\t", 1)[0].decode()
            value = state["values"][item_id]
            if key == b"tab":
                copy_text(value, state.get("clipboard", "auto"))
                status = "copied"
            elif key == b"enter":
                target = state["target"]
                argv = [state["rex"]]
                if target.get("server"):
                    argv += ["-S", target["server"]]
                payload = {"session_id": target["session_id"], "block_id": target["block_id"],
                           "event": {"type": "paste", "payload": {"text": value}}}
                subprocess.run(argv + ["api", "call", "block.event", json.dumps(payload)],
                               check=True, stdout=subprocess.PIPE)
                status = "inserted"
            else:
                raise RuntimeError(f"Unexpected selection key: {key!r}")
            result = {"status": status, "text": value, "count": len(state["items"])}
        # Only explicit experiments write a result file; normal use records no terminal text.
        if state.get("result_file"):
            Path(state["result_file"]).write_text(json.dumps(result, ensure_ascii=False) + "\n")
        return 0
    finally:
        state_path.unlink(missing_ok=True)
        state_path.parent.rmdir()


if __name__ == "__main__":
    sys.exit(main())
