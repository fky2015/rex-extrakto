"""Check the installed Rust helper against the retained Python reference."""
import importlib.util
import json
from pathlib import Path
import random
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("reference", ROOT / "tests/reference/extract.py")
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)
helper = ROOT / "bin" / "rex-extract"
fixtures = [
    "", "\n\n", "old shared\nnew shared",
    "URL: (https://example.com/a_(b)?x=1&y=two).",
    "git@github.com:owner/repo.git ssh://user@host/path file:///tmp/file.txt",
    'open "/tmp/my project/report.md" \'quoted text\' and "/tmp/x"',
    "│ 中文候选：终端文本提取 󰍔 value 󰍔joined │",
    "\x1b[31merror-token\x1b[0m\n\x1b]0;title\x07visible-token",
    "\x1b]0;first\x1b\\ second \x1b\\tail",
    "echo  hello\tworld\r\nnext\vthird\fend\x1cfile\x1dgroup\x1erecord\x1funit",
    "alpha\x85beta\u2028gamma\u2029delta\u00a0epsilon\u3000zeta",
    '5k/s 1/2 ١٢/٣٤ "/tmp/path" "123/456"',
    'foo... ,:;<>"\'|value，。、；：！？ \\"x\\"',
    "\u001c \u001d \u001e \u001f\t\u0085\u2000\u202f\u205f\u3000",
]
random.seed(20261121)
words = ["alpha", "beta", "中文文本", "/tmp/some/path", "https://example.com/q?x=1", "󰍔", "[]", "$", '"/tmp/a b"']
separators = [" ", "\t", "\n", "\r\n", "\u0085", "\u2028", "\u3000"]
for _ in range(10):
    fixtures.append("".join(random.choice(words) + random.choice(separators) for _ in range(30)))
checked = 0
for text in fixtures:
    for mode in ["word", "line", "path", "url", "quote"]:
        for cap in [3, 2000]:
            expected = reference.extract(text, mode=mode, max_lines=10, max_items=cap)
            actual = json.loads(subprocess.check_output(
                [str(helper), "--mode", mode, "--max-lines", "10", "--max-items", str(cap)], input=text.encode()))
            assert expected == actual, (mode, cap, expected, actual)
            checked += 1
for text in fixtures:
    expected = reference.extract(text, max_lines=100)
    titles = [item["title"] for item in expected["items"]]
    records = (("\0".join(titles) + "\0") if titles else "").encode()
    actual = subprocess.check_output([str(helper), "--format", "records", "--max-lines", "100"], input=text.encode())
    assert records == actual, (records, actual)
result = {"json_cases": checked, "records_cases": len(fixtures), "all_equal": True}
artifacts = ROOT / "target/test-results"
artifacts.mkdir(parents=True, exist_ok=True)
(artifacts / "equivalence.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
