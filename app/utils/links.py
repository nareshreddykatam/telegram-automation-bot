"""Link detection for group moderation.

Detection is intentionally layered:
1. explicit URL schemes (http, https, tg, ftp, mailto)
2. www. prefixes
3. bare domains with a conservative TLD allow-list
4. common obfuscations such as example[.]com
5. Telegram message entities (including text_link where the URL is hidden)
"""

from __future__ import annotations

import re
from collections.abc import Sequence

# TLD allow-list keeps normal prose such as "version 1.2" from matching.
_TLDS = (
    "com|net|org|io|me|co|in|uk|us|ru|de|fr|es|it|nl|ca|au|br|jp|cn|"
    "xyz|info|biz|app|dev|site|online|store|link|club|live|tv|ai|gg|ly|"
    "to|cc|ws|pro|top|shop|tech|space|website|fun|icu|vip|win|bid|loan|"
    "download|stream|gdn|men|work|click|pw|su|tk|ml|ga|cf|gq|dog|page|"
    "news|blog|cloud|email|network|digital|agency|media|social|today|world"
)

_URL_RE = re.compile(
    r"(?i)(?<![\w@])(?:"
    r"https?://[^\s<>\"']+"
    r"|ftp://[^\s<>\"']+"
    r"|tg://[^\s<>\"']+"
    r"|mailto:[^\s<>\"']+"
    r")"
)
_WWW_RE = re.compile(r"(?i)(?<![\w@])www\.[a-z0-9][^\s<>\"']*")
_TELEGRAM_RE = re.compile(r"(?i)(?<![\w@])(?:t\.me|telegram\.me|telegram\.dog)/[^\s<>\"']*")
_EMAIL_RE = re.compile(
    r"(?i)(?<![\w.+-])[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[a-z0-9-]+\.)+(?:" + _TLDS + r")(?![\w.-])"
)
_DOMAIN_RE = re.compile(
    r"(?i)(?<![\w@])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:" + _TLDS + r")(?![\w-])(?::\d{2,5})?(?:/[^\s<>\"']*)?"
)
_OBFUSCATED_RE = re.compile(
    r"(?i)(?<![\w@])(?:[a-z0-9-]+(?:\[\.\]|\(\.\)|\(dot\)))+[a-z]{2,24}"
    r"(?![a-z0-9-])"
)

_URL_ENTITY_TYPES = frozenset({"url", "text_link"})
_EMAIL_ENTITY_TYPES = frozenset({"email"})


def detect_links(text: str | None, entities: Sequence[object] | None = None) -> list[str]:
    """Return unique detected link-like strings from text and entities."""

    found: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        cleaned = value.strip().rstrip(".,;:!?)]}'\"")
        if not cleaned:
            return
        key = cleaned.lower()
        if key not in seen:
            seen.add(key)
            found.append(cleaned)

    if text:
        for pattern in (
            _URL_RE,
            _TELEGRAM_RE,
            _WWW_RE,
            _OBFUSCATED_RE,
            _EMAIL_RE,
            _DOMAIN_RE,
        ):
            for match in pattern.finditer(text):
                add(match.group(0))

    for entity in entities or ():
        entity_type = getattr(entity, "type", None)
        value = getattr(entity_type, "value", entity_type)
        if value in _URL_ENTITY_TYPES:
            url = getattr(entity, "url", None) or "text_link"
            add(str(url))
        elif value in _EMAIL_ENTITY_TYPES:
            add("email-entity")

    # A full URL also matches the bare-domain pattern. Keep only the longest
    # representation of each link.
    return [
        item
        for item in found
        if not any(
            item.lower() != other.lower() and item.lower() in other.lower() for other in found
        )
    ]


def message_has_link(message: object) -> bool:
    text = getattr(message, "text", None) or getattr(message, "caption", None)
    entities = getattr(message, "entities", None) or getattr(message, "caption_entities", None)
    return bool(detect_links(text, entities))
