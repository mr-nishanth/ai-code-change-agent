"""Git object identifiers computed locally (SHA-1 object format), so every object GitHub creates
can be compared with what the approved manifest says it must be."""

import hashlib
from collections.abc import Mapping
from typing import cast

TREE_MODE = "40000"


def _object(kind: str, body: bytes) -> str:
    return hashlib.sha1(
        f"{kind} {len(body)}".encode() + b"\0" + body, usedforsecurity=False
    ).hexdigest()


def blob_sha(data: bytes) -> str:
    return _object("blob", data)


def tree_sha(entries: Mapping[str, tuple[str, str]]) -> str:
    """entries: repository-relative file path -> (mode, blob SHA). Directories are implied."""
    root: dict[str, object] = {}
    for path, (mode, sha) in entries.items():
        node = root
        *dirs, leaf = path.split("/")
        for d in dirs:
            child = node.setdefault(d, {})
            if not isinstance(child, dict):
                msg = "file and directory share a name"
                raise ValueError(msg)
            node = child
        node[leaf] = (mode, sha)
    return _tree(root)


def _tree(node: dict[str, object]) -> str:
    items: list[tuple[str, str, str]] = []
    for name, value in node.items():
        if isinstance(value, dict):
            items.append((name + "/", TREE_MODE, _tree(value)))
        else:
            leaf = cast("tuple[str, str]", value)
            items.append((name, leaf[0], leaf[1]))
    items.sort(key=lambda i: i[0].encode())
    body = b"".join(
        f"{mode} {name.rstrip('/')}".encode() + b"\0" + bytes.fromhex(sha)
        for name, mode, sha in items
    )
    return _object("tree", body)


def commit_sha(
    *, tree: str, parent: str, name: str, email: str, timestamp: int, message: str
) -> str:
    who = f"{name} <{email}> {timestamp} +0000"
    body = f"tree {tree}\nparent {parent}\nauthor {who}\ncommitter {who}\n\n{message}"
    return _object("commit", body.encode())
