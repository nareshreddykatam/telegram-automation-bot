"""User-facing command validation errors."""

from __future__ import annotations


class CommandError(Exception):
    """Expected user input/configuration error; safe to show to the admin."""
