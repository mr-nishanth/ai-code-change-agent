"""Central secret detection and redaction.

One scanner is used by artifact storage (redact or refuse) and, later, by validation (secret scan of
a diff). Patterns are deliberately conservative: a false positive blocks or masks, a false negative
leaks, so unclear cases are treated as secrets.
"""

import re
from dataclasses import dataclass

REDACTION_VERSION = "1"
MARKER = "[REDACTED:{kind}]"

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
        ),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA)[0-9A-Z]{16}\b")),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    ),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9]{24,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("url_credentials", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]{1,64}:[^\s/@]{3,}@")),
    (
        "assigned_secret",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|token)"
            r"[\"']?\s*[:=]\s*[\"']?([^\s\"'#,;]{8,})"
        ),
    ),
)
# A value that is a code expression (call, subscript, dotted attribute) is a reference to a secret,
# not the secret itself: `token = get_token()`, `password = config.password`.
_EXPRESSION = re.compile(r"[A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)+|.*[()\[\]].*")
# Values that look like placeholders in docs and tests are not secrets.
_PLACEHOLDER = re.compile(
    r"(?i)^(?:<.*>|\$\{.*\}|\{\{.*\}\}|x{4,}|\*{4,}|changeme|example|placeholder|none|null|true|false)"
)


@dataclass(frozen=True)
class SecretFinding:
    kind: str
    start: int
    end: int


def find_secrets(text: str) -> list[SecretFinding]:
    found: list[SecretFinding] = []
    for kind, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            span = m.span(1) if kind == "assigned_secret" else m.span()
            if kind == "assigned_secret" and (
                _PLACEHOLDER.match(m.group(1)) or _EXPRESSION.fullmatch(m.group(1))
            ):
                continue
            found.append(SecretFinding(kind, *span))
    found.sort(key=lambda f: (f.start, -f.end))
    merged: list[SecretFinding] = []
    for f in found:  # drop findings contained in an earlier one
        if merged and f.start < merged[-1].end:
            continue
        merged.append(f)
    return merged


def redact_text(text: str) -> tuple[str, int]:
    findings = find_secrets(text)
    out: list[str] = []
    last = 0
    for f in findings:
        out.append(text[last : f.start])
        out.append(MARKER.format(kind=f.kind))
        last = f.end
    out.append(text[last:])
    return "".join(out), len(findings)
