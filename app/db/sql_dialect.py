"""Translate the repository layer's SQLite SQL to PostgreSQL.

The repository layer is written once, against SQLite syntax. This module
performs the small, well-understood set of rewrites required by PostgreSQL:

* ``?`` placeholders become ``$1``, ``$2`` ...
* ``INSERT OR IGNORE INTO`` becomes ``INSERT INTO ... ON CONFLICT DO NOTHING``.
* Scalar ``MAX(a, b)`` / ``MIN(a, b)`` become ``GREATEST`` / ``LEAST``.
  Aggregate calls such as ``MAX(id)`` are preserved.
* SQLite's implicit ``rowid`` becomes the explicit ``insert_seq`` identity
  column defined for ``publishing_runs``. Only a ``rowid`` qualified by that
  table or one of its aliases is rewritten, plus an unqualified ``rowid`` when
  ``publishing_runs`` is the statement's only table. A qualified ``rowid`` that
  belongs to any other table is always left untouched.

Every rewrite runs against :func:`app.db.sql_scanner.mask_sql`, so string
literals, quoted identifiers and comments are never modified. Keeping the
rewrites here (instead of forking every repository) means the SQLite and
PostgreSQL backends execute the same repository code.
"""

from __future__ import annotations

import re
from typing import Final

from app.db.sql_scanner import mask_sql, statement_end

# Tables whose primary key is a generated identity column. Only these get an
# automatic ``RETURNING id`` so ``cursor.lastrowid`` keeps working.
ID_TABLES: Final[frozenset[str]] = frozenset(
    {
        "admins",
        "source_chats",
        "source_messages",
        "destinations",
        "publishing_history",
        "moderation_logs",
        "join_request_logs",
    }
)

_ROWID = re.compile(r"\browid\b", re.IGNORECASE)
_IDENT = r"[A-Za-z_][A-Za-z0-9_$]*"
_ALIAS_STOPWORDS = frozenset(
    {
        "as",
        "cross",
        "except",
        "fetch",
        "full",
        "group",
        "having",
        "inner",
        "intersect",
        "join",
        "left",
        "limit",
        "natural",
        "offset",
        "on",
        "order",
        "outer",
        "returning",
        "right",
        "set",
        "union",
        "using",
        "where",
    }
)
_ALIAS_STOPWORD_PATTERN = "|".join(sorted(_ALIAS_STOPWORDS))
_TABLE_REF = re.compile(
    rf"\b(?:FROM|JOIN)\s+(?P<table>{_IDENT}(?:\s*\.\s*{_IDENT})?)"
    rf"(?:\s+(?:AS\s+)?"
    rf"(?P<alias>(?!(?:{_ALIAS_STOPWORD_PATTERN})\b){_IDENT}))?",
    re.IGNORECASE,
)
_QUALIFIED_ROWID = re.compile(
    rf"(?P<qualifier>{_IDENT})\s*\.\s*(?P<rowid>rowid)\b", re.IGNORECASE
)
_INSERT_OR_IGNORE = re.compile(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)
_INSERT_INTO = re.compile(r"\bINSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_RETURNING = re.compile(r"\bRETURNING\b", re.IGNORECASE)
_SCALAR_FUNCTION = re.compile(r"\b(MAX|MIN)\s*\(", re.IGNORECASE)


def to_postgres(sql: str) -> str:
    """Return ``sql`` rewritten for PostgreSQL."""

    translated = _rewrite_rowid(sql)
    translated = _rewrite_insert_or_ignore(translated)
    translated = _rewrite_scalar_functions(translated)
    return _rewrite_placeholders(translated)


def add_returning_id(sql: str) -> tuple[str, bool]:
    """Append ``RETURNING id`` to inserts against identity tables.

    Returns ``(sql, has_returning)``. The second value tells the caller whether
    ``lastrowid`` can be produced from the result rows. Detection is done on the
    masked SQL, so ``RETURNING`` inside a literal or comment does not suppress
    the clause.
    """

    masked = mask_sql(sql)
    match = _INSERT_INTO.search(masked)
    if match is None:
        return sql, False
    table = match.group(1).lower()
    if table not in ID_TABLES:
        return sql, False
    if _RETURNING.search(masked):
        return sql, True
    return _append_clause(sql, "RETURNING id"), True


def _append_clause(sql: str, clause: str) -> str:
    """Append ``clause`` before any trailing semicolon or comment."""

    end = statement_end(sql)
    head = sql[:end]
    tail = sql[end:]
    suffix = ""
    if head.endswith(";"):
        head = head[:-1].rstrip()
        suffix = ";"
    return f"{head} {clause}{suffix}{tail}"


def _rewrite_rowid(sql: str) -> str:
    masked = mask_sql(sql)
    references = list(_TABLE_REF.finditer(masked))
    publishing_aliases: set[str] = set()
    for reference in references:
        table = "".join(reference.group("table").lower().split())
        if table.rsplit(".", 1)[-1] != "publishing_runs":
            continue
        publishing_aliases.add("publishing_runs")
        alias = reference.group("alias")
        if alias is not None:
            publishing_aliases.add(alias.lower())

    # Only publishing_runs has an ``insert_seq`` replacement column. Rewriting
    # every ``rowid`` would silently corrupt joins that also reference another
    # table's ``rowid``.
    if not publishing_aliases:
        return sql

    spans: list[tuple[int, int]] = []
    for match in _QUALIFIED_ROWID.finditer(masked):
        if match.group("qualifier").lower() in publishing_aliases:
            spans.append(match.span("rowid"))

    # An unqualified ``rowid`` is safe to rewrite only when publishing_runs is
    # the statement's sole table. With a join, leaving it untouched fails loudly
    # on PostgreSQL instead of silently selecting insert_seq for the wrong table.
    if len(references) == 1:
        for match in _ROWID.finditer(masked):
            if not masked[: match.start()].rstrip().endswith("."):
                spans.append(match.span())

    spans.sort()
    return _replace_spans(sql, spans, "insert_seq")


def _rewrite_insert_or_ignore(sql: str) -> str:
    masked = mask_sql(sql)
    match = _INSERT_OR_IGNORE.search(masked)
    if match is None:
        return sql
    # Never delete a comment that sits between the keywords. Repository SQL
    # never writes this, so leaving it unchanged is the safe failure mode.
    span = sql[match.start() : match.end()]
    if mask_sql(span) != span:
        return sql
    rewritten = f"{sql[: match.start()]}INSERT INTO{sql[match.end() :]}"
    if "on conflict" in mask_sql(rewritten).lower():
        return rewritten
    return _append_clause(rewritten, "ON CONFLICT DO NOTHING")


def _rewrite_scalar_functions(sql: str) -> str:
    masked = mask_sql(sql)
    edits: list[tuple[int, int, str]] = []
    index = 0
    length = len(sql)

    while index < length:
        match = _SCALAR_FUNCTION.match(masked, index)
        if match is None or (
            index > 0 and (masked[index - 1].isalnum() or masked[index - 1] == "_")
        ):
            index += 1
            continue

        close, commas = _scan_arguments(masked, match.end())
        if close is None or commas == 0:
            index = match.end()
            continue

        replacement = "GREATEST" if match.group(1).upper() == "MAX" else "LEAST"
        edits.append((match.start(1), match.end(1), replacement))
        index = close + 1

    return _apply_edits(sql, edits)


def _scan_arguments(sql: str, start: int) -> tuple[int | None, int]:
    """Find the closing paren for a call body and count top-level commas."""

    depth = 1
    commas = 0
    index = start
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index, commas
        elif char == "," and depth == 1:
            commas += 1
        index += 1
    return None, 0


def _rewrite_placeholders(sql: str) -> str:
    masked = mask_sql(sql)
    out: list[str] = []
    last = 0
    param = 0
    for index, char in enumerate(masked):
        if char != "?":
            continue
        param += 1
        out.append(sql[last:index])
        out.append(f"${param}")
        last = index + 1
    out.append(sql[last:])
    return "".join(out)


def _replace_spans(sql: str, spans: list[tuple[int, int]], replacement: str) -> str:
    if not spans:
        return sql
    out: list[str] = []
    last = 0
    for start, end in spans:
        out.append(sql[last:start])
        out.append(replacement)
        last = end
    out.append(sql[last:])
    return "".join(out)


def _apply_edits(sql: str, edits: list[tuple[int, int, str]]) -> str:
    if not edits:
        return sql
    out: list[str] = []
    last = 0
    for start, end, replacement in edits:
        out.append(sql[last:start])
        out.append(replacement)
        last = end
    out.append(sql[last:])
    return "".join(out)
