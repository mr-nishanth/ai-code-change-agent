"""Runs the profile's validation commands in the fresh validation workspace.

Commands come only from the published repository profile, are re-checked against the platform
command policy, and run in a network-less container. Install commands and network commands are
never run here."""

from ai_code_change_agent.application.workspaces.errors import CommandBlockedError
from ai_code_change_agent.application.workspaces.models import WorkspaceHandle
from ai_code_change_agent.application.workspaces.policy import authorise_command
from ai_code_change_agent.application.workspaces.ports import WorkspaceRuntime
from ai_code_change_agent.domain.repositories.command_policy import CommandKind, CommandPolicy
from ai_code_change_agent.domain.repositories.profile import RepositoryProfile
from ai_code_change_agent.validation.schemas import CommandRecord

VALIDATION_KINDS = (CommandKind.FORMAT_CHECK, CommandKind.LINT, CommandKind.BUILD, CommandKind.TEST)


async def run_commands(
    runtime: WorkspaceRuntime,
    handle: WorkspaceHandle,
    profile: RepositoryProfile,
    policy: CommandPolicy,
) -> list[CommandRecord]:
    records: list[CommandRecord] = []
    ordered = sorted(
        (c for c in profile.commands if c.kind in VALIDATION_KINDS),
        key=lambda c: VALIDATION_KINDS.index(c.kind),
    )
    for spec in ordered:
        try:
            approved = authorise_command(profile, policy, spec.id)
        except CommandBlockedError as exc:
            records.append(
                CommandRecord(
                    command_id=spec.id,
                    kind=spec.kind.value,
                    status="BLOCKED",
                    exit_code=None,
                    duration_ms=0,
                    output_tail=exc.code,
                )
            )
            continue
        result = await runtime.run_command(handle, approved)
        tail = (result.stdout + b"\n" + result.stderr).decode("utf-8", errors="replace")[-8000:]
        records.append(
            CommandRecord(
                command_id=spec.id,
                kind=spec.kind.value,
                status=result.status.value,
                exit_code=result.exit_code,
                duration_ms=result.duration_ms,
                output_tail=tail,
            )
        )
    return records
