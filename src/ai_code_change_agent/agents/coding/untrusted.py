"""Framing for untrusted text that enters a prompt or a tool result.

Every block of external text (Story fields, repository content, contracts, validation output,
search results, fetched pages) is wrapped between a BEGINS and an ENDS line that carry a random
id chosen for that block. The text cannot predict the id, so it cannot close the frame early;
the system prompt tells the model that only the ENDS line with the same id ends the data.
"""

import re
import secrets
import unicodedata

_MEDIA_TYPE = re.compile(r"[a-z0-9.+-]{1,40}/[a-z0-9.+-]{1,60}")
_NOT_CODE = re.compile(r"[^a-z0-9_.:-]")
_CODE_LIMIT = 120
_LABEL_LIMIT = 200


def frame_untrusted(label: str, text: str) -> str:
    """Wrap text in a frame with a fresh random id. label should be platform text; it is
    still flattened to one printable line (at most 200 characters) so it can never end the
    BEGINS line early."""
    label = one_line(
        "".join(" " if unicodedata.category(c)[0] in "CZ" else c for c in label), _LABEL_LIMIT
    )
    boundary = secrets.token_hex(8)
    while boundary in text or boundary in label:
        boundary = secrets.token_hex(8)
    return f"UNTRUSTED DATA {boundary} BEGINS: {label}\n{text}\nUNTRUSTED DATA {boundary} ENDS"


def one_line(text: str, limit: int) -> str:
    """Collapse all whitespace (including newlines) to single spaces and cut to limit."""
    return " ".join(text.split())[:limit]


def safe_media_type(value: str) -> str:
    """A plain type/subtype, or text/plain when the server sent anything else."""
    candidate = value.strip().lower()
    return candidate if _MEDIA_TYPE.fullmatch(candidate) else "text/plain"


def safe_code(value: str) -> str:
    """A refusal code reduced to [a-z0-9_.:-]; codes can carry a redirect host or a provider
    error code, so they never reach the model verbatim."""
    code = _NOT_CODE.sub("_", one_line(value, _CODE_LIMIT).lower())
    return code or "unknown"
