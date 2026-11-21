"""Wait for real terminal output rather than a fixed startup delay."""
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
