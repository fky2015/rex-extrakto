"""Print a pane fixture, then record raw input without executing it."""
import os
from pathlib import Path
import sys
import termios
import tty

destination = Path(sys.argv[1])
destination.write_bytes(b"")
sys.stdout.buffer.write(Path(sys.argv[2]).read_bytes())
sys.stdout.buffer.flush()
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
