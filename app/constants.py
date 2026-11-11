"""Shared constants and status values."""

from __future__ import annotations

# Publishing run statuses (stored in publishing_runs and returned to handlers).
RUN_RUNNING = "running"
RUN_COMPLETED = "completed"
RUN_COMPLETED_WITH_ERRORS = "completed_with_errors"
RUN_INCOMPLETE = "incomplete"
RUN_PAUSED = "paused"
RUN_DISABLED = "disabled"
RUN_NO_MESSAGES = "no_messages"
RUN_FAILED = "failed"

SUCCESSFUL_RUN_STATUSES = frozenset({RUN_COMPLETED, RUN_COMPLETED_WITH_ERRORS, RUN_NO_MESSAGES})

# Per-message publishing history statuses.
MSG_SENDING = "sending"
MSG_SENT = "sent"
MSG_FAILED = "failed"
MSG_UNCERTAIN = "uncertain"
MSG_RETRY = "retry"

# Scheduled-run statuses.
SCHED_RUNNING = "running"
SCHED_COMPLETED = "completed"
SCHED_FAILED = "failed"

# Join request log actions.
JOIN_APPROVED = "approved"
JOIN_FAILED = "failed"
JOIN_IGNORED = "ignored"
JOIN_DECLINED = "declined"

# Telegram's built-in anonymous group administrator account id.
TELEGRAM_ANONYMOUS_ADMIN_ID = 1087968824

# Telegram Bot API limits used for safety.
MAX_BATCH_SIZE_HARD_LIMIT = 10_000
MAX_MEDIA_GROUP_SIZE = 10
HISTORY_PAGE_SIZE_DEFAULT = 10
HISTORY_PAGE_SIZE_MAX = 50
