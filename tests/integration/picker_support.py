"""Wait for real terminal output rather than a fixed startup delay."""
import json
import time


def wait_for_picker(cli, session, block, *expected):
    deadline = time.monotonic() + 5
    while True:
        screen = cli(["capture", "-s", session, "-b", block, "--trim", "--unwrap"]).stdout.decode()
        if all(text in screen for text in expected):
            return screen
        if time.monotonic() >= deadline:
            raise RuntimeError("Picker did not become ready: " + screen)
        time.sleep(0.02)


def wait_for_block_removal(cli, session, block):
    """Process completion can precede removal of its floating layer."""
    deadline = time.monotonic() + 5
    while True:
        view = json.loads(cli(["session", "inspect", session, "--json"]).stdout)
        blocks = {entry["block_id"] for window in view["windows"]
                  for layer in window["layers"] for entry in layer["blocks"]}
        if block not in blocks:
            return view
        if time.monotonic() >= deadline:
            raise RuntimeError("Picker pane was not removed: " + block)
        time.sleep(0.02)
