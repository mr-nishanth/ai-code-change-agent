"""Parses a GitHub repository reference written in a Jira Story into the registry locator.

Accepted: `owner/name` and `https://github.com/owner/name`, each with an optional `.git` suffix
(the URL also with one optional trailing slash). Everything else is refused: other schemes and
hosts, SSH forms, user info, ports, query strings, fragments, percent-encoding, extra path segments
(`/tree/...`, `/blob/...`, `/pull/...`) and malformed names. This is pure string handling: the
value is never fetched, resolved or used as a URL; only the resulting locator string reaches a
registry lookup.
"""

import re

PROVIDER_KEY = "github"
WEB_PREFIX = "https://github.com/"
_SLUG = re.compile(r"([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,100})")
_FORBIDDEN = re.compile(r"[@?#%\\\s\x00-\x1f\x7f]")


def locator_for(owner: str, name: str) -> str:
    return f"{PROVIDER_KEY}:{owner.lower()}/{name.lower()}"


class GitHubReferenceParser:
    def locator(self, reference: str) -> str | None:
        value = reference.strip()
        if not value or len(value) > 300 or _FORBIDDEN.search(value):
            return None
        if value.lower().startswith(WEB_PREFIX):
            value = value[len(WEB_PREFIX) :]
            if value.endswith("/"):
                value = value[:-1]
        elif ":" in value:
            return None  # any other scheme, host:path or SSH form
        match = _SLUG.fullmatch(value)
        if match is None:
            return None
        owner, name = match.groups()
        if name.lower().endswith(".git"):
            name = name[: -len(".git")]
        if name in {"", ".", ".."}:
            return None
        return locator_for(owner, name)
