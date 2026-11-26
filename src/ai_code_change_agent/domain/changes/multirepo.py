"""Multi-repository Stories: parsing the declarations and ordering the repositories.

A Story declares repositories with explicit markers only (free text is never read):

    Repositories:
    - Repository: <owner>/backend-api
      Role: backend
      Responsibility: create tracking endpoints and response schemas
      Depends on: none
    - Repository: <owner>/web-app
      Role: frontend
      Depends on: backend-api

An optional "Dependencies:" section may add edges as lines "- <a> depends on <b>[, <c>]".
A single `Repository:` marker keeps the single-repository path unchanged. Declarations only
NAME repositories; the registry still authorises each one (ACTIVE, project mapping, profile)."""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

_REF = r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}"
_MARKER = re.compile(rf"^\s*(?:[-*\d.)]+\s*)?(?:repository|repo)\s*:\s*({_REF})\s*$", re.I)
_FIELD = re.compile(
    r"^\s*(?:[-*]\s*)?(role|responsibility|depends\s*on|depends_on|requirements?)\s*:\s*(.*)$",
    re.I,
)
_EDGE = re.compile(rf"^\s*(?:[-*]\s*)?({_REF})\s+depends\s+on\s+(.+?)\s*$", re.I)
_SECTION = re.compile(r"^\s*[A-Za-z][A-Za-z ]{0,40}:\s*$")
_NONE = {"", "none", "-", "n/a", "nothing"}
MAX_TEXT = 2000


class DeclarationError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class RepositoryDeclaration:
    reference: str
    role: str | None = None
    responsibility: str | None = None
    depends_on: list[str] = field(default_factory=list)
    requirements: str | None = None


def _clean_ref(value: str) -> str:
    return value.strip().rstrip(".,:;/-")


def _split_refs(value: str) -> list[str]:
    parts = [_clean_ref(p) for p in re.split(r"[,;]|\band\b", value)]
    return [p for p in parts if p and p.lower() not in _NONE]


def _parse(text: str) -> tuple[list[RepositoryDeclaration], list[tuple[str, list[str]]]]:
    """Every `Repository:` marker with its indented Role/Responsibility/Depends on fields, plus
    edges from a Dependencies section. Duplicate references are merged."""
    out: list[RepositoryDeclaration] = []
    edges: list[tuple[str, list[str]]] = []
    current: RepositoryDeclaration | None = None
    in_dependencies = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if m := _MARKER.match(line):
            current = RepositoryDeclaration(_clean_ref(m.group(1)))
            out.append(current)
            in_dependencies = False
            continue
        if re.match(r"^\s*dependencies\s*:\s*$", line, re.I):
            in_dependencies, current = True, None
            continue
        if in_dependencies and (e := _EDGE.match(line)):
            edges.append((_clean_ref(e.group(1)), _split_refs(e.group(2))))
            continue
        if current is not None and (f := _FIELD.match(line)):
            name, value = f.group(1).lower().replace(" ", "_"), f.group(2).strip()[:MAX_TEXT]
            if name == "role":
                current.role = value[:40] or None
            elif name == "responsibility":
                current.responsibility = value or None
            elif name in ("depends_on",):
                current.depends_on.extend(_split_refs(value))
            else:
                current.requirements = value or None
            continue
        if _SECTION.match(line):
            current, in_dependencies = None, False
    merged: dict[str, RepositoryDeclaration] = {}
    for d in out:
        key = d.reference.lower()
        if key in merged:
            prev = merged[key]
            prev.role = prev.role or d.role
            prev.responsibility = prev.responsibility or d.responsibility
            prev.depends_on.extend(x for x in d.depends_on if x not in prev.depends_on)
        else:
            merged[key] = d
    return list(merged.values()), edges


def parse_story(text: str) -> list[RepositoryDeclaration]:
    declarations, edges = _parse(text)
    by_ref = {d.reference.lower(): d for d in declarations}
    for source, targets in edges:
        target = by_ref.get(source.lower()) or _match_name(source, declarations)
        if target is None:
            raise DeclarationError("dependency_unknown_repository", source)
        target.depends_on.extend(t for t in targets if t not in target.depends_on)
    return declarations


def _short(ref: str) -> str:
    return ref.rsplit("/", 1)[-1].lower().replace("_", "-").removesuffix(".git")


def _match_name(
    name: str, declarations: Iterable[RepositoryDeclaration]
) -> RepositoryDeclaration | None:
    hits = [d for d in declarations if _short(d.reference) == _short(name)]
    return hits[0] if len(hits) == 1 else None


def dependency_order(nodes: Sequence[str], edges: Mapping[str, Sequence[str]]) -> list[str]:
    """Topological order (Kahn); raises on an unknown node or a cycle. edges: node -> needs."""
    known = set(nodes)
    for node, wanted in edges.items():
        for need in wanted:
            if need not in known:
                raise DeclarationError("dependency_unknown_repository", need)
            if need == node:
                raise DeclarationError("dependency_cycle", node)
    remaining = {n: set(edges.get(n, ())) for n in nodes}
    order: list[str] = []
    while remaining:
        ready = sorted(n for n, needs in remaining.items() if not needs)
        if not ready:
            raise DeclarationError("dependency_cycle", ", ".join(sorted(remaining)))
        for n in ready:
            order.append(n)
            del remaining[n]
        for needs in remaining.values():
            needs.difference_update(ready)
    return order
