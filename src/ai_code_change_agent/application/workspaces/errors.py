"""Workspace errors. Messages are fixed codes: never paths, content or command output."""


class WorkspaceError(Exception):
    code = "workspace_error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code)


class UnsafePathError(WorkspaceError, ValueError):
    code = "unsafe_path"


class ProtectedPathError(WorkspaceError):
    code = "protected_path"


class UnsupportedEntryError(WorkspaceError):
    """A symlink, device, socket or other non-regular entry: refused, never followed."""

    code = "unsupported_file_entry"


class WorkspaceExistsError(WorkspaceError):
    code = "workspace_exists"


class WorkspaceNotFoundError(WorkspaceError):
    code = "workspace_not_found"


class WorkspaceLimitError(WorkspaceError):
    code = "workspace_limit_exceeded"


class InvalidChangeError(WorkspaceError):
    code = "invalid_change"


class CommandBlockedError(WorkspaceError):
    """The command is not permitted (policy, network or unknown id): it is not run."""

    code = "command_blocked"


class BaselineError(WorkspaceError):
    """A pinned baseline could not be read and fully verified. Never a partial snapshot: the code
    names the reason (identity, installation access, unsupported entry, limit, integrity...)."""

    code = "baseline_error"

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WorkspaceRuntimeError(WorkspaceError):
    """The container runtime failed or is unavailable."""

    code = "workspace_runtime_failed"
