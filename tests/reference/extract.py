#!/usr/bin/env python3
"""Python behavior reference for Rust equivalence tests; not used at runtime."""

import argparse
import json
import re
import sys

DECORATION = r"\u2500-\u27bf\ue000-\uf8ff⋅↴"
WORD = re.compile(r"[^\s\[\](){}=$，。、；：（）「」《》！？" + DECORATION + r"]+")
URL = re.compile(r"(?:https?://|ssh://|git://|ftp://|file:///|git@)[^\s<>\"'" + DECORATION + r"]+")
QUOTED = re.compile(r'''"([^"\r\n]+)"|'([^'\r\n]+)' ''', re.VERBOSE)
ANSI = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")


def clean(value):
    value = value.strip(",:;<>\"'|，。、；：！？")
    # Remove prose punctuation while keeping balanced parentheses inside URLs.
    value = value.rstrip(".")
    for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
        while value.endswith(closing) and value.count(closing) > value.count(opening):
            value = value[:-1]
    return value


def kind(value):
    if re.match(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*://|git@)", value):
        return "URL"
    if "/" in value and not re.match(r"^(?:\d+/\d+|[\d.]*[kmgKMG]?/s)$", value):
        return "Path"
    return "Word"


def extract(text, mode="word", max_lines=3000, max_items=2000, min_length=3):
    if mode not in ("word", "line", "path", "url", "quote"):
        raise ValueError("mode must be word, line, path, url, or quote")
    if not 1 <= max_items <= 2000 or max_lines < 1 or min_length < 1:
        raise ValueError("max_items must be 1..2000; max_lines and min_length must be positive")
    lines = ANSI.sub("", text).splitlines()[-max_lines:]
    seen, items, values = set(), [], {}
    limited = False
    for line in reversed(lines):
        if mode == "line":
            candidates = [(line.strip(), "Line")]
        elif mode == "url":
            candidates = [(clean(m.group()), "URL") for m in URL.finditer(line)]
        elif mode == "quote":
            candidates = [(m.group(1) or m.group(2), "Quote") for m in QUOTED.finditer(line)]
        else:
            # Keep URLs (including query '=' and parentheses) and quoted paths whole.
            candidates = [(clean(m.group()), "URL") for m in URL.finditer(line)]
            candidates += [(m.group(1) or m.group(2), "Path")
                           for m in QUOTED.finditer(line) if kind(m.group(1) or m.group(2)) == "Path"]
            candidates += [(clean(m.group()), None) for m in WORD.finditer(line)]
        for value, label in candidates:
            label = label or kind(value)
            if mode == "path" and label != "Path":
                continue
            if len(value) < min_length or value in seen:
                continue
            seen.add(value)
            if len(items) == max_items:
                limited = True
                break
            item_id = str(len(items) + 1)
            items.append({"id": item_id, "title": value, "detail": label})
            values[item_id] = value
        if limited:
            break
    return {"items": items, "values": values, "source_lines": len(lines), "limited": limited}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="word")
    parser.add_argument("--max-lines", type=int, default=3000)
    parser.add_argument("--max-items", type=int, default=2000)
    parser.add_argument("--min-length", type=int, default=3)
    args = parser.parse_args()
    result = extract(sys.stdin.read(), args.mode, args.max_lines, args.max_items, args.min_length)
    json.dump(result, sys.stdout, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main()
