"""Small SQL-aware lexer shared by the dialect layer.

Only lexical structure is needed here: rewrites must know which bytes are
*code*, *string literals*, *quoted identifiers* or *comments* so that data,
column names and comments are never rewritten.

The lexer deliberately does not try to be a complete SQL parser. It covers the
constructs the repository layer actually uses (plus PostgreSQL dollar quoting)
and fails safe: anything it does not understand stays in the code stream.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

CODE = "code"
STRING = "string"
IDENTIFIER = "identifier"
COMMENT = "comment"

_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


@dataclass(frozen=True, slots=True)
class Segment:
    """A contiguous span of SQL with a single lexical kind."""

    kind: str
    start: int
    end: int


def iter_segments(sql: str) -> Iterator[Segment]:
    """Yield alternating code / literal / comment segments covering ``sql``."""

    index = 0
    length = len(sql)
    code_start = 0

    while index < length:
        char = sql[index]
        end: int | None = None
        kind = CODE

        if char == "'":
            end = _skip_quoted(sql, index)
            kind = STRING
        elif char in {'"', "`"}:
            end = _skip_quoted(sql, index)
            kind = IDENTIFIER
        elif char == "[":
            close = sql.find("]", index + 1)
            end = length if close == -1 else close + 1
            kind = IDENTIFIER
        elif sql.startswith("--", index):
            newline = sql.find("\n", index)
            end = length if newline == -1 else newline
            kind = COMMENT
        elif sql.startswith("/*", index):
            close = sql.find("*/", index + 2)
            end = length if close == -1 else close + 2
            kind = COMMENT
        elif char == "$":
            match = _DOLLAR_TAG.match(sql, index)
            if match is not None:
                close = sql.find(match.group(0), match.end())
                end = length if close == -1 else close + len(match.group(0))
                kind = STRING

        if end is None:
            index += 1
            continue

        if code_start < index:
            yield Segment(CODE, code_start, index)
        yield Segment(kind, index, end)
        index = code_start = end

    if code_start < length:
        yield Segment(CODE, code_start, length)


def mask_sql(sql: str) -> str:
    """Return ``sql`` with everything outside code replaced by spaces.

    The result has exactly the same length and line breaks as ``sql``, so
    positions found in the masked text can be applied to the original.
    """

    masked = list(sql)
    for segment in iter_segments(sql):
        if segment.kind == CODE:
            continue
        for position in range(segment.start, segment.end):
            if masked[position] not in "\r\n":
                masked[position] = " "
    return "".join(masked)


def statement_end(sql: str) -> int:
    """Return the offset just past the last non-comment token.

    Trailing whitespace and trailing comments are excluded so a clause can be
    appended without landing inside a comment or literal.
    """

    end = 0
    for segment in iter_segments(sql):
        if segment.kind != COMMENT:
            end = segment.end
    while end > 0 and sql[end - 1].isspace():
        end -= 1
    return end


def _skip_quoted(sql: str, start: int) -> int:
    """Return the offset after a single- or double-quoted span.

    Doubling the quote character escapes it, matching SQLite and PostgreSQL.
    """

    quote = sql[start]
    index = start + 1
    length = len(sql)
    while index < length:
        if sql[index] == quote:
            if index + 1 < length and sql[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    return length


__all__ = [
    "CODE",
    "COMMENT",
    "IDENTIFIER",
    "STRING",
    "Segment",
    "iter_segments",
    "mask_sql",
    "statement_end",
]
