"""A raw terminal receiver for checking paste bytes without running commands."""
import os
from pathlib import Path
import sys
import termios
import tty

destination = Path(sys.argv[1])
destination.write_bytes(b"")
print("rex-insert-test-20261120", flush=True)
print('echo "你好" --target=/tmp/example', flush=True)
fd = sys.stdin.fileno()
previous = termios.tcgetattr(fd)
try:
    tty.setraw(fd)
    while True:
        data = os.read(fd, 65536)
        if data in (b"\x03", b"\x04"):
            break
        with destination.open("ab") as output:
            output.write(data)
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, previous)
