"""Build the Slack payload for the slack-release-announce action.

Reads TITLE, RELEASE_URL and RELEASE_BODY from the environment (action.yml
binds the action's inputs to them) and prints the JSON payload for the
incoming webhook.

The release notes are GitHub-flavoured markdown assembled from merged commit
subjects, so they are text written by any contributor to the calling repo.
Slack reads `<...>` in mrkdwn as a link or a special mention (`<!channel>`,
`<!subteam^ID>`, `<@UID>`), so the conversion treats the notes as untrusted:
`&`, `<` and `>` are entity-escaped everywhere, and the only `<...>` the
output can contain is a link whose target is an http(s) URL free of `<`, `>`
and `|`. A markdown link to anything else (`[x](!channel)`, `[x](@U123)`,
`[x](javascript:...)`) is emitted as escaped text.

Standard library only; tests/test_slack_release_announce.py imports it.
"""

from __future__ import annotations

import json
import os
import re

# A markdown link `[label](target)`.
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
# The only targets that may become a Slack link: an http(s) URL with none of
# the characters that would end or restructure a `<target|label>` sequence.
SAFE_TARGET = re.compile(r"^https?://[^\s<>|]+$")
HEADING = re.compile(r"^#{1,6}\s+(.*)$")
BULLET = re.compile(r"^(\s*)[*-]\s+")

# Slack section text caps ~3000 chars; keep headroom.
MAX_BODY = 2900
TRUNCATED = "… _(truncated — see full release below)_"


def escape(text: str) -> str:
    """Escape the three characters Slack mrkdwn reads as control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def convert_line(line: str) -> str:
    """Convert one markdown line to mrkdwn; every `<` in the result opens an http(s) link."""
    parts: list[str] = []
    end = 0
    for m in LINK.finditer(line):
        parts.append(escape(line[end : m.start()]))
        label, target = m.groups()
        if SAFE_TARGET.match(target):
            parts.append(f"<{escape(target)}|{escape(label)}>")
        else:
            parts.append(escape(m.group(0)))
        end = m.end()
    parts.append(escape(line[end:]))
    line = "".join(parts)
    heading = HEADING.match(line)
    if heading:
        return f"*{heading.group(1)}*"
    return BULLET.sub(r"\1• ", line)


def convert(text: str) -> list[str]:
    """Convert GitHub-flavoured markdown release notes to Slack mrkdwn, line by line."""
    return [convert_line(line) for line in text.split("\n")]


def section_text(release_body: str) -> str:
    """The converted notes, cut at the Slack section limit."""
    kept: list[str] = []
    total = 0
    for line in convert(release_body):
        if total + len(line) + 1 > MAX_BODY:
            kept.append(TRUNCATED)
            break
        kept.append(line)
        total += len(line) + 1
    return "\n".join(kept) or "_(no release notes)_"


def payload(title: str, release_url: str, release_body: str) -> dict:
    return {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": section_text(release_body)}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"<{release_url}|View the full release>"}},
        ]
    }


def main() -> None:
    print(json.dumps(payload(os.environ["TITLE"], os.environ["RELEASE_URL"], os.environ.get("RELEASE_BODY", ""))))


if __name__ == "__main__":
    main()
