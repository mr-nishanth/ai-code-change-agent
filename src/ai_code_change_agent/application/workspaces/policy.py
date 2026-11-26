"""Turning a profile command into an `ApprovedCommand`. The only constructor path."""

from ai_code_change_agent.application.workspaces.errors import CommandBlockedError
from ai_code_change_agent.application.workspaces.models import ApprovedCommand
from ai_code_change_agent.domain.repositories.command_policy import CommandPolicy
from ai_code_change_agent.domain.repositories.profile import RepositoryProfile


def authorise_command(
    profile: RepositoryProfile, policy: CommandPolicy, command_id: str
) -> ApprovedCommand:
    """Look the command up in the published profile (never in model output) and check it against
    the platform policy again, now. Commands that need the network are not supported in 05B."""
    spec = next((c for c in profile.commands if c.id == command_id), None)
    if spec is None:
        raise CommandBlockedError("command_not_in_profile")
    if spec.network != "none":
        raise CommandBlockedError("network_commands_unsupported")
    findings = policy.check(
        field_path=f"commands.{spec.id}",
        family=spec.toolchain,
        kind=spec.kind,
        argv=spec.argv,
        cwd=spec.cwd,
        timeout_seconds=spec.timeout_seconds,
        env=spec.env,
        network_requested=False,
    )
    if findings:
        raise CommandBlockedError("command_rejected_by_policy")
    return ApprovedCommand(
        command_id=spec.id,
        family=spec.toolchain,
        kind=spec.kind,
        argv=spec.argv,
        cwd=spec.cwd,
        timeout_seconds=spec.timeout_seconds,
        env=dict(spec.env),
    )
