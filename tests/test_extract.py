import unittest
import json
from pathlib import Path
import subprocess

HELPER = Path(__file__).resolve().parents[1] / "bin" / "rex-extract"


def invoke(text, timeout=30, **kwargs):
    args = [str(HELPER)]
    for key, value in kwargs.items():
        args.extend(["--" + key.replace("_", "-"), str(value)])
    payload = text.encode() if isinstance(text, str) else text
    return subprocess.run(
        args,
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


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

    def test_balanced_bracket_pairs_are_preserved(self):
        for url in (
            "https://example.com/a_(b)?x=1&y=two",
            "https://example.com/a[0]",
            "https://example.com/a_(b)[c]",
        ):
            with self.subTest(url=url):
                self.assertEqual(titles(f"({url}).", mode="url"), [url])

    def test_unbalanced_trailing_brackets_are_stripped(self):
        cases = {
            "https://example.com/a)": "https://example.com/a",
            "https://example.com/a))": "https://example.com/a",
            "https://example.com/a]": "https://example.com/a",
            "https://example.com/a]]": "https://example.com/a",
            "https://example.com/a_(b))": "https://example.com/a_(b)",
            "https://example.com/a[0]]": "https://example.com/a[0]",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(titles(raw, mode="url"), [expected])

    def test_intermixed_bracket_suffixes_follow_pair_order(self):
        cases = {
            "https://example.com/a[0])": "https://example.com/a[0]",
            "https://example.com/a(b]]": "https://example.com/a(b",
            "https://example.com/a(b)c)": "https://example.com/a(b)c",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(titles(raw, mode="url"), [expected])

    def test_large_unbalanced_suffix_stays_bounded_and_correct(self):
        url = "https://example.com/path"
        try:
            result = invoke(url + "(" + ")" * 400_000, mode="url", max_items=1, timeout=15)
        except subprocess.TimeoutExpired:
            self.fail("cleanup of a large bracket suffix exceeded 15s; it is likely quadratic again")
        result.check_returncode()
        self.assertEqual(
            [item["title"] for item in json.loads(result.stdout)["items"]],
            [url + "()"],
        )

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
