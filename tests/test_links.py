from __future__ import annotations

import pytest

from app.utils.links import detect_links, message_has_link
from tests.helpers import FakeChat, FakeEntity, FakeMessage


@pytest.mark.parametrize(
    "text",
    [
        "please visit https://example.com/path",
        "join t.me/+abcdef",
        "telegram.me/somechannel",
        "go to www.example.org now",
        "mail me at person@example.com",
        "obfuscated example[.]com here",
        "check ftp://files.example.net/file",
        "tg://resolve?domain=test",
    ],
)
def test_common_link_formats_are_detected(text: str):
    assert detect_links(text)


@pytest.mark.parametrize(
    "text",
    [
        "version 1.2 is ready",
        "the price is 100.50",
        "no links in this message",
        "readme.txt is not a domain",
        "hello world",
    ],
)
def test_non_links_are_not_detected(text: str):
    assert not detect_links(text)


def test_text_link_entity_is_detected_even_without_visible_url():
    message = FakeMessage(
        chat=FakeChat(-1001),
        message_id=1,
        from_user=None,
        text="click here",
        entities=[FakeEntity(type="text_link", url="https://hidden.example")],
    )
    assert message_has_link(message)


def test_url_entity_is_detected():
    message = FakeMessage(
        chat=FakeChat(-1001),
        message_id=1,
        from_user=None,
        text="example",
        entities=[FakeEntity(type="url", url=None)],
    )
    assert message_has_link(message)
