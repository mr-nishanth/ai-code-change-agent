"""Atlassian Document Format to safe plain text.

The renderer is pure and bounded (depth and node limits). Output is untrusted data. Media are
never fetched; unknown node types keep their children and are reported, not dropped.
"""

import re
from typing import Any

from ai_code_change_agent.application.jira.models import JiraRichText

MAX_DEPTH = 40
MAX_NODES = 50_000
_BLANKS = re.compile(r"\n{3,}")
_BLOCK_CONTAINERS = frozenset(
    {
        "doc",
        "blockquote",
        "panel",
        "expand",
        "nestedExpand",
        "tableCell",
        "tableHeader",
        "mediaSingle",
        "mediaGroup",
        "layoutSection",
        "layoutColumn",
        "listItem",
    }
)
_INLINE_CONTAINERS = frozenset({"paragraph", "heading", "codeBlock", "caption"})


class _State:
    def __init__(self) -> None:
        self.nodes = 0
        self.flags: dict[str, None] = {}

    def flag(self, name: str) -> None:
        self.flags.setdefault(name, None)


def to_rich_text(value: object) -> JiraRichText | None:
    if value is None:
        return None
    if isinstance(value, str):
        return JiraRichText(text=value)
    if isinstance(value, dict) and value.get("type") == "doc":
        state = _State()
        text = _BLANKS.sub("\n\n", _render(value, state, 0)).strip()
        return JiraRichText(text=text, structure=value, unsupported_nodes=tuple(state.flags))
    return JiraRichText(text="", unsupported_nodes=("non_adf_value",))


def _children(node: dict[str, Any]) -> list[Any]:
    content = node.get("content")
    return content if isinstance(content, list) else []


def _attr(node: dict[str, Any], *names: str) -> str:
    attrs = node.get("attrs")
    if isinstance(attrs, dict):
        for name in names:
            candidate = attrs.get(name)
            if isinstance(candidate, str) and candidate:
                return candidate
    return ""


def _inline(node: dict[str, Any], state: _State, depth: int) -> str:
    return "".join(_render(child, state, depth + 1) for child in _children(node))


def _blocks(node: dict[str, Any], state: _State, depth: int) -> str:
    parts = (_render(child, state, depth + 1) for child in _children(node))
    return "\n".join(p for p in parts if p)


def _list(node: dict[str, Any], state: _State, depth: int, *, ordered: bool) -> str:
    start = 1
    attrs = node.get("attrs")
    if ordered and isinstance(attrs, dict) and isinstance(attrs.get("order"), int):
        start = attrs["order"]
    lines: list[str] = []
    for offset, item in enumerate(_children(node)):
        prefix = f"{start + offset}. " if ordered else "- "
        body = _render(item, state, depth + 1).split("\n")
        lines.append(prefix + body[0])
        lines.extend(" " * len(prefix) + line for line in body[1:])
    return "\n".join(lines)


def _render(node: object, state: _State, depth: int) -> str:
    if not isinstance(node, dict):
        return ""
    state.nodes += 1
    if state.nodes > MAX_NODES:
        state.flag("node_limit_exceeded")
        return ""
    if depth > MAX_DEPTH:
        state.flag("depth_limit_exceeded")
        return ""
    kind = node.get("type")
    if kind == "text":
        text = node.get("text")
        return text if isinstance(text, str) else ""
    if kind == "hardBreak":
        return "\n"
    if kind == "rule":
        return "---"
    if kind == "mention":
        return _attr(node, "text") or "@mention"
    if kind == "emoji":
        return _attr(node, "text", "shortName")
    if kind in ("inlineCard", "blockCard", "embedCard"):
        return _attr(node, "url")
    if kind == "status":
        return _attr(node, "text")
    if kind == "date":
        return _attr(node, "timestamp")
    if kind == "media":
        return "[media]"
    if kind in _INLINE_CONTAINERS:
        return _inline(node, state, depth)
    if kind in _BLOCK_CONTAINERS:
        return _blocks(node, state, depth)
    if kind == "bulletList":
        return _list(node, state, depth, ordered=False)
    if kind == "orderedList":
        return _list(node, state, depth, ordered=True)
    if kind == "tableRow":
        return " | ".join(_render(c, state, depth + 1) for c in _children(node))
    if kind == "table":
        return _blocks(node, state, depth)
    state.flag(kind if isinstance(kind, str) and kind else "unknown")
    return _blocks(node, state, depth)
