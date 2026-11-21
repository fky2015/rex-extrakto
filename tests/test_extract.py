import unittest
import json
from pathlib import Path
import subprocess

HELPER = Path(__file__).resolve().parents[1] / "bin" / "rex-extract"


def invoke(text, **kwargs):
    args = [str(HELPER)]
    for key, value in kwargs.items():
        args.extend(["--" + key.replace("_", "-"), str(value)])
    payload = text.encode() if isinstance(text, str) else text
    return subprocess.run(args, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def extract(text, **kwargs):
    result = invoke(text, **kwargs)
    result.check_returncode()
    return json.loads(result.stdout)


def titles(text, **kwargs):
    return [item["title"] for item in extract(text, **kwargs)["items"]]


class ExtractionTests(unittest.TestCase):
    def test_preserves_url_query_and_balanced_parentheses(self):
        url = "https://example.com/a_(b)?x=1&y=two"
        self.assertIn(url, titles(f"URL: ({url})."))
        self.assertEqual(titles(f"({url}).", mode="url"), [url])

    def test_quoted_path_with_spaces(self):
        path = "/tmp/my project/report.md"
        self.assertIn(path, titles(f'open "{path}"'))
        self.assertIn(path, titles(f'open "{path}"', mode="path"))

    def test_unicode_and_decorative_terminal_symbols(self):
        result = titles("│ 终端文本提取 󰍔 中文候选：提取结果 │")
        self.assertIn("终端文本提取", result)
        self.assertIn("中文候选", result)
        self.assertNotIn("󰍔", result)

    def test_recent_lines_first_and_deduplicated(self):
        self.assertEqual(titles("older shared\nnewer shared"), ["newer", "shared", "older"])

    def test_recent_line_limit(self):
        self.assertEqual(titles("old\nrecent", max_lines=1), ["recent"])

    def test_picker_cap_and_unique_ids(self):
        result = extract("alpha beta gamma", max_items=2)
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(result["limited"])
        self.assertEqual(len(result["values"]), 2)

    def test_empty_output(self):
        self.assertEqual(titles("  \n │ \n"), [])

    def test_line_keeps_internal_whitespace(self):
        self.assertEqual(titles("  echo  hello\tworld  ", mode="line"), ["echo  hello\tworld"])

    def test_ansi_removed(self):
        self.assertEqual(titles("\x1b[31merror-token\x1b[0m"), ["error-token"])

    def test_invalid_limits_and_mode(self):
        for options in ({"max_items": 2001}, {"max_lines": 0}, {"min_length": 0}, {"mode": "unknown"}):
            with self.subTest(options=options):
                result = invoke("test", **options)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertTrue(result.stderr)

    def test_invalid_utf8_reports_error(self):
        result = invoke(b"\xff")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")

    def test_invalid_output_format_reports_error(self):
        result = invoke("test", format="invalid")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")


if __name__ == "__main__":
    unittest.main()
