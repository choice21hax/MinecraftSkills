"""A tolerant SNBT tokenizer/parser for FTB Quests files (and other SNBT configs).

Two entry points:

* :func:`parse` returns plain Python data for analysis (quest graphs, item references).
* :func:`rewrite_strings` rewrites the *contents of string tokens only*, leaving every other byte of the
  file untouched, so transformations are format-aware and diffs stay minimal.

FTB Quests SNBT uses newlines as separators (commas optional), bare keys and numeric suffixes.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any


class SnbtError(ValueError):
    pass


@dataclass
class Token:
    kind: str  # punct | string | word
    value: str
    start: int
    end: int
    quote: str = ""


_WORD = re.compile(r"[A-Za-z0-9_\-.+:/]+")
_NUM = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?([bBsSlLfFdD])?$")


def tokenize(text: str) -> Iterator[Token]:
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n,":
            i += 1
            continue
        if c in "{}[]:;":
            yield Token("punct", c, i, i + 1)
            i += 1
            continue
        if c in "\"'":
            j, buf = i + 1, []
            while j < n and text[j] != c:
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            if j >= n:
                raise SnbtError(f"unterminated string at offset {i}")
            yield Token("string", "".join(buf), i, j + 1, quote=c)
            i = j + 1
            continue
        m = _WORD.match(text, i)
        if not m:
            raise SnbtError(f"unexpected character {c!r} at offset {i}")
        # a bare word followed by ':' is a key; ':' inside ids only occurs in quoted strings in FTB files
        word = m.group(0)
        if word.endswith(":"):
            word = word[:-1]
        yield Token("word", word, i, i + len(word))
        i += len(word)


def _scalar(word: str) -> Any:
    if word == "true":
        return True
    if word == "false":
        return False
    m = _NUM.match(word)
    if m:
        body = word[:-1] if m.group(3) else word
        try:
            return (
                float(body) if ("." in body or "e" in body.lower() or m.group(3) in ("f", "F", "d", "D")) else int(body)
            )
        except ValueError:
            return word
    return word


def parse(text: str) -> Any:
    toks = list(tokenize(text))
    pos = 0

    def value() -> Any:
        nonlocal pos
        if pos >= len(toks):
            raise SnbtError("unexpected end of input")
        t = toks[pos]
        if t.kind == "punct" and t.value == "{":
            pos += 1
            out: dict[str, Any] = {}
            while True:
                if pos >= len(toks):
                    raise SnbtError("unterminated compound")
                if toks[pos].kind == "punct" and toks[pos].value == "}":
                    pos += 1
                    return out
                key_tok = toks[pos]
                if key_tok.kind == "punct":
                    raise SnbtError(f"expected key at offset {key_tok.start}")
                pos += 1
                if pos >= len(toks) or toks[pos].value != ":":
                    raise SnbtError(f"expected ':' after key {key_tok.value!r}")
                pos += 1
                out[key_tok.value] = value()
        if t.kind == "punct" and t.value == "[":
            pos += 1
            # typed arrays: [I; 1, 2]
            if pos + 1 < len(toks) and toks[pos].kind == "word" and toks[pos + 1].value == ";":
                pos += 2
            items: list[Any] = []
            while True:
                if pos >= len(toks):
                    raise SnbtError("unterminated list")
                if toks[pos].kind == "punct" and toks[pos].value == "]":
                    pos += 1
                    return items
                items.append(value())
        if t.kind == "string":
            pos += 1
            return t.value
        if t.kind == "word":
            pos += 1
            return _scalar(t.value)
        raise SnbtError(f"unexpected {t.value!r} at offset {t.start}")

    result = value()
    if pos != len(toks):
        raise SnbtError(f"trailing content at offset {toks[pos].start}")
    return result


def _escape(s: str, quote: str) -> str:
    return s.replace("\\", "\\\\").replace(quote, "\\" + quote)


def rewrite_strings(text: str, fn: Callable[[str], str | None]) -> tuple[str, list[tuple[int, str, str]]]:
    """Apply ``fn`` to each string token's value; ``None`` keeps the original.

    Returns the new text and a list of (offset, old, new) changes.
    """
    out, last, changes = [], 0, []
    for t in tokenize(text):
        if t.kind != "string":
            continue
        new = fn(t.value)
        if new is None or new == t.value:
            continue
        out.append(text[last : t.start])
        out.append(f"{t.quote}{_escape(new, t.quote)}{t.quote}")
        last = t.end
        changes.append((t.start, t.value, new))
    out.append(text[last:])
    return "".join(out), changes
