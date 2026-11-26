"""Changeset extraction: what the implementation phase changed, compared with the pinned baseline.

The inspection is done by the platform on the host copy of the sandbox workspace (never by the
agent). The result is a deterministic, hashable list of file changes."""

import hashlib
import json
from collections.abc import Mapping, Sequence

from ai_code_change_agent.application.workspaces.models import (
    ChangeOp,
    FileChange,
    TreeEntry,
    TreeManifest,
)


def changed_paths(baseline: TreeManifest, work: TreeManifest) -> dict[str, ChangeOp]:
    before = {e.path: e for e in baseline.entries}
    after = {e.path: e for e in work.entries}
    ops: dict[str, ChangeOp] = {}
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path), after.get(path)
        if new is None:
            ops[path] = ChangeOp.DELETE
        elif old is None:
            ops[path] = ChangeOp.CREATE
        elif (old.sha256, old.executable) != (new.sha256, new.executable):
            ops[path] = ChangeOp.MODIFY
    return ops


def changeset(ops: Mapping[str, ChangeOp], contents: Mapping[str, bytes]) -> tuple[FileChange, ...]:
    """File changes from `changed_paths` plus the work-tree bytes of created/modified files."""
    return tuple(
        FileChange(path, op, None if op is ChangeOp.DELETE else contents[path])
        for path, op in sorted(ops.items())
    )


def expected_manifest(baseline: TreeManifest, changes: Sequence[FileChange]) -> TreeManifest:
    """The tree a fresh baseline copy must have after the changes are applied."""
    entries = {e.path: e for e in baseline.entries}
    for c in changes:
        if c.op is ChangeOp.DELETE:
            entries.pop(c.path, None)
            continue
        assert c.content is not None  # noqa: S101 - create and modify carry content
        previous = entries.get(c.path)
        entries[c.path] = TreeEntry(
            c.path,
            len(c.content),
            hashlib.sha256(c.content).hexdigest(),
            previous.executable if previous else False,
        )
    return TreeManifest(tuple(sorted(entries.values(), key=lambda e: e.path)))


def changeset_sha256(changes: Sequence[FileChange]) -> str:
    """Hash over (path, operation, content hash), independent of order of discovery."""
    rows = [
        [
            c.path,
            c.op.value,
            hashlib.sha256(c.content).hexdigest() if c.content is not None else None,
        ]
        for c in sorted(changes, key=lambda c: c.path)
    ]
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
