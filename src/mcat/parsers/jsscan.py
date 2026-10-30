"""Minimal JavaScript lexer for KubeJS scripts: finds string literals, skipping comments and regexes.

It is used to read and rewrite identifiers that appear as *whole string literals* (e.g.
``'create:andesite_alloy'``). Template literals containing ``${`` are reported but never rewritten.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass


@dataclass
class JsString:
    value: str  # raw source text between the quotes (escapes untouched)
    start: int  # offset of opening quote
    end: int  # offset after closing quote
    quote: str
    line: int
    interpolated: bool = False


_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^") | {""}


def scan_strings(src: str) -> Iterator[JsString]:
    i, n, line = 0, len(src), 1
    prev = ""  # last significant non-space character
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c in " \t\r":
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += src.count("\n", i, j)
            i = j
            continue
        if c in "'\"`":
            j, start_line, interp = i + 1, line, False
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 2
                    continue
                if c == "`" and src.startswith("${", j):
                    interp = True
                if src[j] == "\n":
                    line += 1
                    if c != "`":
                        break  # unterminated ordinary string: stop at line end
                j += 1
            yield JsString(src[i + 1 : j], i, min(j + 1, n), c, start_line, interp)
            i = j + 1
            prev = c
            continue
        if c == "/" and prev in _REGEX_PREV:
            j, in_class = i + 1, False
            while j < n and src[j] != "\n":
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "[":
                    in_class = True
                elif src[j] == "]":
                    in_class = False
                elif src[j] == "/" and not in_class:
                    break
                j += 1
            i = j + 1
            prev = "/"
            continue
        prev = c if not (c.isalnum() or c in "_$") else "a"
        i += 1


def rewrite_strings(src: str, fn: Callable[[str], str | None]) -> tuple[str, list[tuple[int, str, str]]]:
    """Replace whole string literals whose value ``fn`` maps to something new. Returns (text, changes)."""
    out, last, changes = [], 0, []
    for s in scan_strings(src):
        if s.interpolated or "\\" in s.value:
            continue
        new = fn(s.value)
        if new is None or new == s.value:
            continue
        out.append(src[last : s.start])
        out.append(f"{s.quote}{new}{s.quote}")
        last = s.end
        changes.append((s.line, s.value, new))
    out.append(src[last:])
    return "".join(out), changes
