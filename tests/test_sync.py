from pathlib import Path

from wactx.sync import parse_export


SAMPLE_CHAT = """\
3/10/26, 9:00 AM - Alice: Hello everyone
3/10/26, 9:01 AM - Bob: Hey Alice!
3/10/26, 9:02 AM - Alice: Multi-line
message here
3/10/26, 9:03 AM - Charlie: Got it
"""


def test_parse_export(tmp_path):
    p = tmp_path / "chat.txt"
    p.write_text(SAMPLE_CHAT)

    chat_name, messages = parse_export(p)
    assert chat_name == "chat"
    assert len(messages) == 4
    assert messages[0]["sender"] == "Alice"
    assert messages[0]["text"] == "Hello everyone"
    assert messages[2]["text"] == "Multi-line\nmessage here"


def test_parse_export_24h_format(tmp_path):
    p = tmp_path / "group.txt"
    p.write_text(
        "15/03/2026, 14:30 - User1: test message\n15/03/2026, 14:31 - User2: reply\n"
    )

    chat_name, messages = parse_export(p)
    assert len(messages) == 2
    assert messages[0]["sender"] == "User1"
    assert messages[0]["timestamp"].hour == 14


def test_parse_export_empty(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("This is not a WhatsApp export\n")

    chat_name, messages = parse_export(p)
    assert len(messages) == 0
