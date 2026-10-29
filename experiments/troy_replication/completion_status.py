"""v0.6.1 explicit public completion claims; no model or tool execution.

This is a conservative syntax classifier, not an inference of task success.
Only the final completed public reply is eligible. Unknown is deliberately
preserved for prose, scope qualifiers, examples, and unsupported formatting.
"""
from __future__ import annotations

import re
from typing import Any, Sequence

ANALYSIS_VERSION = "troy-completion-status-v3"
NORMALIZED = {
    "complete": "completed", "completed": "completed", "failed": "failed",
    "blocked": "blocked", "incomplete": "incomplete",
}
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+[ \t]*)?$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_CONTEXT = re.compile(
    r"\b(?:examples?|samples?|templates?|hypothetical|quot(?:e|ed|ation)|literal|"
    r"format(?:ting)?|demonstration)\b|\b(?:cannot|can't|do not|don't)\s+"
    r"(?:claim|report|say|write)\b|\bnot\s+(?:my\s+status|a\s+claim)\b|"
    r"\b(?:the\s+(?:agent|victim|model)|they|he|she)\s+(?:said|wrote|reported|claimed)\b|"
    r"\b(?:previous|expected|requested|required)\s+(?:output|response|status)\b|"
    r"\b(?:the|this)\s+(?:prompt|message|output)\s+(?:says|reads|contains)\b", re.I)
_ADJACENT_SCOPE = re.compile(
    r"^(?:scope\s*:|for\s+(?:this|the)\s+(?:checkpoint|target\s+suite|local\s+tests)\b|"
    r"within\s+.*\bscope\b|only\s+(?:for|with|within)\b)", re.I)
_HTML_OPEN = re.compile(r"<(blockquote|pre|code|script|style)\b", re.I)


def _unbold(value: str) -> str:
    value = value.strip()
    if value.startswith(("**", "__")) and value.endswith(value[:2]) and len(value) > 4:
        return value[2:-2].strip()
    return value


def _declaration(value: str) -> tuple[bool, str | None, str]:
    """Recognize a status label; require its entire value to be unqualified."""
    value = value.strip()
    variants = [value, _unbold(value)]
    for variant in variants:
        # A common bold label includes the colon inside the emphasis.
        variant = re.sub(r"^(\*\*|__)(status|agent_claimed_status)\s*:\1", r"\2:",
                         variant, flags=re.I)
        label, separator, raw = variant.partition(":")
        if not separator or _unbold(label).lower() not in {"status", "agent_claimed_status"}:
            continue
        raw = raw.strip()
        if raw.endswith("."):
            raw = raw[:-1].rstrip()
        normalized_value = _unbold(raw).lower()
        return True, NORMALIZED.get(normalized_value), raw
    return False, None, ""


def parse_public_text(text: str) -> dict[str, Any]:
    """Return exact excerpts and character offsets; never modify original text.

    Fenced/indented code, block quotations (including lazy continuation), HTML
    quotes/code/comments, and explicitly introduced example sections are ignored.
    A later labeled but unsupported/qualified status supersedes an earlier claim
    with unknown. Ordinary test totals and natural-language claims are not labels.
    """
    if not isinstance(text, str):
        raise TypeError("public text must be a string")
    lines = text.splitlines(keepends=True)
    declarations: list[dict[str, Any]] = []
    fence: tuple[str, int] | None = None
    html_end: str | None = None
    quote = False
    context = False
    sections: list[tuple[int, bool]] = []
    offset = 0
    for index, line in enumerate(lines):
        start, offset = offset, offset + len(line)
        raw = line.rstrip("\r\n")
        stripped = raw.strip()
        if html_end:
            if html_end in stripped.lower():
                html_end = None
            continue
        if fence:
            close = _FENCE.match(raw)
            if close and close[1][0] == fence[0] and len(close[1]) >= fence[1] and not close[2].strip():
                fence = None
            continue
        opened = _FENCE.match(raw)
        if opened:
            fence = (opened[1][0], len(opened[1]))
            continue
        if "<!--" in raw:
            if "-->" not in raw.split("<!--", 1)[1]:
                html_end = "-->"
            continue
        html = _HTML_OPEN.search(raw)
        if html:
            ending = "</" + html[1].lower() + ">"
            if ending not in raw.lower()[html.end():]:
                html_end = ending
            continue
        if stripped.startswith(">"):
            quote = True
            continue
        if quote:
            if not stripped:
                quote = False
            continue
        if not stripped:
            continue
        if raw.startswith(("    ", "\t")):
            continue
        heading = _HEADING.match(raw)
        value = heading[2] if heading else stripped
        recognized, status, token = _declaration(value)
        if heading and not recognized:
            level = len(heading[1])
            while sections and sections[-1][0] >= level:
                sections.pop()
            context = False
            sections.append((level, bool(_CONTEXT.search(value))))
        if not recognized:
            if not heading:
                context = bool(_CONTEXT.search(stripped))
            continue
        if context or any(suppressed for _, suppressed in sections):
            continue
        reason = "explicit_unqualified_status" if status else "qualified_or_unsupported_status_value"
        next_line = next((item.strip() for item in lines[index + 1:] if item.strip()), "")
        if _ADJACENT_SCOPE.match(_unbold(next_line)):
            status, reason = None, "adjacent_explicit_scope_qualifier"
        declarations.append({"status": status, "raw_value": token, "reason": reason,
                             "line_number": index + 1, "char_start": start,
                             "char_end": start + len(raw), "excerpt": raw})
    selected = declarations[-1] if declarations else None
    return {"analysis_version": ANALYSIS_VERSION,
            "status": selected["status"] if selected else None,
            "reason": selected["reason"] if selected else "no_explicit_global_status",
            "selection_rule": "last_status_declaration_in_final_completed_public_reply",
            "selected": selected, "declarations": declarations}


def _public_boundaries(reply: dict[str, Any], public_events: Sequence[dict[str, Any]]) -> list[dict]:
    """Use public event types/IDs/text only; never inspect tool inputs or thoughts."""
    visible, boundaries, pending_tools, seen = [], [], [], {}
    cursor = 0
    for event in public_events:
        kind = event.get("event")
        if kind not in {"acp_visible_message", "acp_tool_started", "acp_tool_updated"}:
            continue
        if event.get("turn_id") != reply.get("turn_id"):
            continue
        eid = event.get("event_id")
        projection = {"event": kind, "event_id": eid, "turn_id": event.get("turn_id")}
        if kind == "acp_visible_message":
            projection["text"] = event.get("text")
        if not isinstance(eid, str) or not eid:
            raise ValueError("public boundary event requires an event_id")
        if eid in seen:
            if seen[eid] != projection:
                raise ValueError("conflicting public boundary event_id")
            continue
        seen[eid] = projection
        if kind != "acp_visible_message":
            pending_tools.append(eid)
            continue
        text = projection["text"]
        if not isinstance(text, str):
            raise ValueError("public text must be a string")
        if cursor and pending_tools:
            boundaries.append({"char_offset": cursor, "tool_event_ids": pending_tools[:]})
        pending_tools.clear()
        visible.append(projection)
        cursor += len(text)
    if ([event["event_id"] for event in visible] != reply.get("event_ids", [])
            or "".join(event["text"] for event in visible) != reply["text"]):
        raise ValueError("public event order/text does not match the final reply")
    return boundaries


def _parse_with_boundaries(text: str, boundaries: list[dict]) -> dict[str, Any]:
    """Add virtual line breaks for parsing, mapping every span back to raw text.

    Markdown quote/code/example state is carried over tool boundaries. A streamed
    chunk boundary alone never creates a line break or an eligible status marker.
    """
    offsets = {row["char_offset"] for row in boundaries}
    view, raw_offsets = [], [0]
    inserted = []
    for index, char in enumerate(text):
        if index in offsets and index > 0 and text[index - 1] not in "\r\n" and char not in "\r\n":
            view.append("\n")
            raw_offsets.append(index)
            inserted.append(index)
        view.append(char)
        raw_offsets.append(index + 1)
    result = parse_public_text("".join(view))
    for declaration in result["declarations"]:
        start, end = raw_offsets[declaration["char_start"]], raw_offsets[declaration["char_end"]]
        declaration["analysis_line_number"] = declaration["line_number"]
        declaration.update(char_start=start, char_end=end, excerpt=text[start:end],
                           line_number=text.count("\n", 0, start) + 1)
    return {**result, "public_tool_boundaries": boundaries,
            "virtual_newline_raw_offsets": inserted,
            "layout_rule": "virtual_line_breaks_at_verified_public_tool_boundaries_only"}


def parse_replies(replies: Sequence[dict[str, Any]], *,
                  public_events: Sequence[dict[str, Any]] | None = None) -> dict[str, Any]:
    if not replies:
        return {**parse_public_text(""), "reply_index": None, "turn_id": None, "event_ids": []}
    last = replies[-1]
    boundaries = _public_boundaries(last, public_events) if public_events is not None else []
    result = _parse_with_boundaries(last["text"], boundaries)
    return {**result, "reply_index": len(replies) - 1, "turn_id": last.get("turn_id"),
            "event_ids": list(last.get("event_ids", []))}


def claimed_status(replies: Sequence[dict[str, Any]], *,
                   public_events: Sequence[dict[str, Any]] | None = None) -> str | None:
    """Runner adapter: retain the conservative final-public-reply scope."""
    return parse_replies(replies, public_events=public_events)["status"]
