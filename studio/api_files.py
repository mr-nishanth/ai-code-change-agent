"""Read-only file browsing over a workspace (work tree vs. baseline). Paths are confined to the
tree and symlinks are never followed."""

import difflib
from pathlib import Path

from fastapi import HTTPException

MAX_FILE_BYTES = 512 * 1024


def safe_path(root: Path, rel: str) -> Path:
    rel = rel.strip().lstrip("/")
    if rel.startswith("workspace/"):
        rel = rel.removeprefix("workspace/")
    target = (root / rel) if rel else root
    if ".." in Path(rel).parts:
        raise HTTPException(400, "path_invalid")
    for parent in [target, *target.parents]:
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise HTTPException(400, "path_invalid")
    if not target.resolve().is_relative_to(root.resolve()):
        raise HTTPException(400, "path_invalid")
    return target


def _listing(root: Path) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    if not root.is_dir() or root.is_symlink():
        return out
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink() or "__pycache__" in rel or ".pytest_cache" in rel:
            continue
        out.append(
            {
                "path": rel,
                "name": p.name,
                "type": "directory" if p.is_dir() else "file",
                "size": p.stat().st_size if p.is_file() else 0,
            }
        )
    return out


def _text(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    data = path.read_bytes()[:MAX_FILE_BYTES]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def tree_view(work: Path, base: Path, sandbox: str) -> dict[str, object]:
    entries = _listing(work)
    base_files = {str(e["path"]) for e in _listing(base) if e["type"] == "file"}
    for e in entries:
        if e["type"] != "file":
            continue
        rel = str(e["path"])
        if rel not in base_files:
            e["change"] = "added"
        elif (work / rel).read_bytes() != (base / rel).read_bytes():
            e["change"] = "modified"
    deleted = sorted(base_files - {str(e["path"]) for e in entries})
    return {"entries": entries, "deleted": deleted, "sandbox": sandbox}


def file_view(work_root: Path, base_root: Path, path: str) -> dict[str, object]:
    content = _text(safe_path(work_root, path))
    original = _text(safe_path(base_root, path))
    if content is None and original is None:
        raise HTTPException(404, "file_not_found_or_binary")
    diff = "".join(
        difflib.unified_diff(
            (original or "").splitlines(keepends=True),
            (content or "").splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    return {"path": path, "content": content, "original": original, "diff": diff}
