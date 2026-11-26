"""Operator CLI: `ai-code-change repositories ...`, `ai-code-change changes ...`,
`ai-code-change worker`.

Administration is CLI-only until operator SSO exists (ADR-P22). Actor attribution is the OS user
and is therefore **unauthenticated**: anyone with database access can run this. The CLI never
prints credentials and never executes profile commands. It contacts a Git host only to verify a
repository's identity read-only while registering it (when a provider client is configured).
"""

import argparse
import asyncio
import getpass
import json
import re
import shlex
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TextIO

from ai_code_change_agent.application.repositories.resolver import RepositoryResolver
from ai_code_change_agent.application.repositories.service import RepositoryRegistryService
from ai_code_change_agent.application.repositories.validation import (
    ProfileValidator,
    parse_document,
)
from ai_code_change_agent.cli import operations
from ai_code_change_agent.core.errors import ConfigurationError
from ai_code_change_agent.domain.changes.errors import ChangeError
from ai_code_change_agent.domain.repositories.catalog import CAPABILITIES, VALIDATION_POLICIES
from ai_code_change_agent.domain.repositories.command_policy import CommandPolicy
from ai_code_change_agent.domain.repositories.errors import (
    Finding,
    RegistryError,
    RegistryValidationError,
)
from ai_code_change_agent.domain.repositories.models import NewRepository
from ai_code_change_agent.domain.repositories.resolution import ResolutionRequest
from ai_code_change_agent.domain.repositories.templates import standard_profile

Services = tuple[RepositoryRegistryService, RepositoryResolver]
ServiceFactory = Callable[[], Services]

EXAMPLES = """examples:
  ai-code-change repositories validate-profile profile.json
  ai-code-change repositories register --key shop-api --display-name "Shop API" \\
      --provider github --provider-id 123456 --owner acme --name shop-api --branch main \\
      --access-ref app-installation:98765
  ai-code-change repositories register --from-provider acme/shop-api --project SHOP --enable
      # one command: register, standard profile (python -m pytest -q), map to SHOP, enable
  ai-code-change repositories init-profile shop-api  # standard profile with its test command
  ai-code-change repositories create-version shop-api profile.json
  ai-code-change repositories publish shop-api 1
  ai-code-change repositories map-project SHOP shop-api
  ai-code-change repositories enable shop-api
  ai-code-change repositories resolve --project SHOP --reference shop-api
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-code-change",
        description="Repository registry administration (CLI only; no HTTP admin API).",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    top = parser.add_subparsers(dest="area", required=True)
    operations.add_parsers(top)
    repo = top.add_parser(
        "repositories",
        help="manage the repository registry",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = repo.add_subparsers(dest="command", required=True)

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_)

    p = add(
        "validate-profile", "validate a profile file offline (executes nothing, needs no database)"
    )
    p.add_argument("file", type=Path)
    p.add_argument("--format", choices=("json", "toml"))
    add("toolchains", "show the platform command policy catalogue")
    p = add(
        "register",
        "register a repository; with --from-provider, --project and --enable this is the whole "
        "onboarding (the standard profile is created automatically)",
    )
    p.add_argument("--key", help="registry key (default: derived from the repository name)")
    p.add_argument("--display-name", help="default: the repository name")
    p.add_argument(
        "--access-ref",
        help="default: app-installation:<GITHUB_APP_INSTALLATION_ID> from the environment",
    )
    p.add_argument("--project", action="append", default=[], help="Jira project key to map")
    p.add_argument("--enable", action="store_true", help="set ACTIVE after the profile exists")
    p.add_argument("--toolchain", default="python", help="toolchain of the standard profile")
    p.add_argument("--no-profile", action="store_true", help="do not create the standard profile")
    for flag in ("provider", "provider-id", "owner", "name", "branch"):
        p.add_argument(f"--{flag}")
    p.add_argument(
        "--from-provider",
        metavar="OWNER/NAME",
        help="read the numeric id, canonical owner/name and default branch from the provider "
        "(read-only); the repository is still created DISABLED, without profile or mapping",
    )
    add("list", "list repositories")
    p = add("show", "inspect a repository, its versions, aliases and mappings")
    p.add_argument("key")
    p = add(
        "init-profile",
        "build the standard profile for a toolchain (with its test command), store it as a new "
        "version and publish it (any repository key; nothing is hard-coded per repository)",
    )
    p.add_argument("key")
    p.add_argument("--toolchain", default="python")
    p.add_argument("--test-command", help='override, e.g. "python -m pytest -q tests"')
    p.add_argument("--draft", action="store_true", help="create the version but do not publish")
    p = add("create-version", "validate a profile file and store it as a new draft version")
    p.add_argument("key")
    p.add_argument("file", type=Path)
    p.add_argument("--format", choices=("json", "toml"))
    p = add("publish", "publish a draft version (immutable afterwards) and activate it")
    p.add_argument("key")
    p.add_argument("version", type=int)
    p.add_argument("--no-activate", action="store_true")
    p = add("activate-version", "activate an already published version (rollback)")
    p.add_argument("key")
    p.add_argument("version", type=int)
    for name, text in (
        ("enable", "set ACTIVE (needs an active profile)"),
        ("disable", "set DISABLED"),
    ):
        p = add(name, text)
        p.add_argument("key")
    p = add("alias-add", "add an alias (global namespace shared with repository keys)")
    p.add_argument("key")
    p.add_argument("alias")
    for name, text in (
        ("map-project", "authorise a Jira project to use a repository"),
        ("unmap-project", "disable that authorisation"),
    ):
        p = add(name, text)
        p.add_argument("project")
        p.add_argument("key")
    p = add("mappings", "list project mappings")
    p.add_argument("--project")
    p.add_argument("--repository")
    p = add("audit", "show registry audit history")
    p.add_argument("--repository")
    p.add_argument("--limit", type=int, default=50)
    p = add("resolve", "dry-run the deterministic resolver (read-only)")
    p.add_argument("--project", required=True)
    p.add_argument("--reference", action="append", default=[])
    return parser


def _actor() -> str:
    name = re.sub(r"[^A-Za-z0-9_.@-]", "_", getpass.getuser())[:64] or "unknown"
    return f"cli:{name}"


def _fmt(path: Path, forced: str | None) -> Literal["json", "toml"]:
    kind = forced or ("toml" if path.suffix == ".toml" else "json")
    return "toml" if kind == "toml" else "json"


def _read(path: Path) -> bytes:
    if not path.is_file():
        raise RegistryValidationError([Finding("file", "not_a_file", "profile file not found")])
    return path.read_bytes()[:300_000]


def _when(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S") if moment else "-"


def _plain(obj: Any) -> Any:
    if isinstance(obj, list | tuple):
        return [_plain(o) for o in obj]
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _plain(v) for k, v in asdict(obj).items()}
    if hasattr(obj, "value"):
        return obj.value
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    return str(obj) if not isinstance(obj, str | int | float | bool | type(None)) else obj


def _emit(out: TextIO, as_json: bool, data: Any, text: str) -> None:
    out.write((json.dumps(_plain(data), indent=2, sort_keys=True) if as_json else text) + "\n")


async def _run(
    args: argparse.Namespace,
    svc: RepositoryRegistryService,
    resolver: RepositoryResolver,
    out: TextIO,
) -> None:
    j: bool = args.json
    cmd: str = args.command
    actor = _actor()
    if cmd == "register":
        _fill_register_defaults(args)
        repo = await svc.register_repository(await _new_repository(args, svc), actor=actor)
        steps = [f"registered {repo.repository_key} ({repo.canonical_locator})"]
        if not args.no_profile:
            v = await svc.create_version(
                repo.repository_key, standard_profile(repo.repository_key, args.toolchain),
                actor=actor,
            )  # fmt: skip
            await svc.publish_version(repo.repository_key, v.version, actor=actor, activate=True)
            steps.append(f"standard {args.toolchain} profile v{v.version} published")
        for project in args.project:
            await svc.map_project(project.upper(), repo.repository_key, actor=actor)
            steps.append(f"mapped to {project.upper()}")
        if args.enable:
            repo = await svc.enable(repo.repository_key, actor=actor)
            steps.append("ACTIVE")
        else:
            steps.append(f"status {repo.status} (add --enable, or run enable when ready)")
        _emit(out, j, repo, "; ".join(steps))
    elif cmd == "list":
        repos = await svc.list_repositories()
        _emit(
            out,
            j,
            repos,
            "\n".join(f"{r.repository_key:<28} {r.status:<9} {r.canonical_locator}" for r in repos)
            or "no repositories",
        )
    elif cmd == "show":
        repo, versions, aliases, mappings = await svc.show_repository(args.key)
        lines = [
            f"{repo.repository_key}: {repo.status}, {repo.canonical_locator}, "
            f"branch {repo.default_branch}, access {repo.access_reference}"
        ]
        lines += [
            f"  version {v.version}: {'published' if v.published else 'draft'}"
            f"{' (ACTIVE)' if v.id == repo.active_profile_version_id else ''} "
            f"sha256 {v.content_sha256[:12]}"
            for v in versions
        ]
        lines += [f"  alias {a}" for a in aliases] + [
            f"  project {m.jira_project_key}: {m.status}" for m in mappings
        ]
        _emit(
            out,
            j,
            {"repository": repo, "versions": versions, "aliases": aliases, "mappings": mappings},
            "\n".join(lines),
        )
    elif cmd == "create-version":
        doc = parse_document(_read(args.file), _fmt(args.file, args.format))
        v = await svc.create_version(args.key, doc, actor=actor)
        _emit(
            out,
            j,
            v,
            f"created draft version {v.version} (sha256 {v.content_sha256[:12]}); "
            "publish it to make it immutable and active",
        )
    elif cmd == "init-profile":
        argv = tuple(shlex.split(args.test_command)) if args.test_command else None
        doc = standard_profile(args.key, args.toolchain, argv)
        v = await svc.create_version(args.key, doc, actor=actor)
        text = (
            f"created version {v.version} (sha256 {v.content_sha256[:12]}) "
            f"from the {args.toolchain} template"
        )
        if not args.draft:
            v = await svc.publish_version(args.key, v.version, actor=actor, activate=True)
            text += "; published and activated"
        _emit(out, j, v, text)
    elif cmd == "publish":
        v = await svc.publish_version(
            args.key, args.version, actor=actor, activate=not args.no_activate
        )
        _emit(
            out,
            j,
            v,
            f"published version {v.version}{'' if args.no_activate else ' and activated it'}",
        )
    elif cmd == "activate-version":
        r = await svc.activate_version(args.key, args.version, actor=actor)
        _emit(out, j, r, f"activated version {args.version} for {r.repository_key}")
    elif cmd in ("enable", "disable"):
        r = await (svc.enable if cmd == "enable" else svc.disable)(args.key, actor=actor)
        _emit(out, j, r, f"{r.repository_key} is now {r.status}")
    elif cmd == "alias-add":
        alias = await svc.add_alias(args.key, args.alias, actor=actor)
        _emit(out, j, {"alias": alias}, f"alias {alias} -> {args.key}")
    elif cmd in ("map-project", "unmap-project"):
        fn = svc.map_project if cmd == "map-project" else svc.unmap_project
        m = await fn(args.project, args.key, actor=actor)
        _emit(out, j, m, f"project {m.jira_project_key} -> {args.key}: {m.status}")
    elif cmd == "mappings":
        ms = await svc.list_mappings(project_key=args.project, key=args.repository)
        _emit(
            out,
            j,
            ms,
            "\n".join(f"{m.jira_project_key:<12} {m.status:<9} {m.repository_id}" for m in ms)
            or "no mappings",
        )
    elif cmd == "audit":
        events = await svc.audit_history(args.repository, args.limit)
        _emit(
            out,
            j,
            events,
            "\n".join(
                f"{_when(e.occurred_at)} {e.actor:<20} {e.action.value:<30} {dict(e.metadata)}"
                for e in events
            )
            or "no audit events",
        )
    elif cmd == "resolve":
        res = await resolver.resolve(
            ResolutionRequest("CLI-DRYRUN", args.project, tuple(args.reference))
        )
        _emit(
            out,
            j,
            res,
            f"{res.outcome.value}: {res.detail}"
            + (
                f" -> {res.repository_key} v{res.profile_version}"
                if res.resolved
                else f" (follow-up: {res.follow_up.value})"
            ),
        )


_MANUAL = ("provider", "provider_id", "owner", "name", "branch")


def _fill_register_defaults(args: argparse.Namespace) -> None:
    """Derive the key, display name and access reference so onboarding needs only
    --from-provider OWNER/NAME (nothing is configured per repository in code)."""
    name = str(args.from_provider or "").partition("/")[2] or (args.name or "")
    if args.key is None:
        key = re.sub(r"[^a-z0-9-]+", "-", name.lower().replace("_", "-")).strip("-")
        if not key:
            raise RegistryValidationError(
                [Finding("register", "arguments_missing", "missing --key (or --from-provider)")]
            )
        args.key = key if key[0].isalpha() else f"r-{key}"
    if args.display_name is None:
        args.display_name = name or args.key
    if args.access_ref is None:
        from ai_code_change_agent.core.config import get_settings

        installation = get_settings().github_app_installation_id
        if installation is None:
            raise RegistryValidationError(
                [Finding("register", "arguments_missing", "missing --access-ref")]
            )
        args.access_ref = f"app-installation:{installation}"


async def _new_repository(
    args: argparse.Namespace, svc: RepositoryRegistryService
) -> NewRepository:
    if args.from_provider is None:
        missing = [f"--{f.replace('_', '-')}" for f in _MANUAL if getattr(args, f) is None]
        if missing:
            raise RegistryValidationError(
                [Finding("register", "arguments_missing", "missing " + ", ".join(missing))]
            )
        return NewRepository(
            args.key,
            args.display_name,
            args.provider,
            args.provider_id,
            args.owner,
            args.name,
            args.branch,
            args.access_ref,
        )
    if any(getattr(args, f) is not None for f in _MANUAL if f != "provider"):
        raise RegistryValidationError(
            [
                Finding(
                    "register", "arguments_conflict", "--from-provider replaces the identity flags"
                )
            ]
        )
    owner, sep, name = str(args.from_provider).partition("/")
    if not sep or "/" in name:
        raise RegistryValidationError(
            [Finding("from_provider", "reference_invalid", "expected OWNER/NAME")]
        )
    provider = args.provider or "github"
    info = await svc.describe_provider_repository(
        provider, owner=owner, name=name, access_reference=args.access_ref
    )
    return NewRepository(
        args.key,
        args.display_name,
        provider,
        info.provider_repository_id,
        info.owner,
        info.name,
        info.default_branch,
        args.access_ref,
    )


def _print_findings(err: RegistryValidationError, err_out: TextIO) -> None:
    for f in err.findings:
        err_out.write(f"  - {f.field}: {f.code}: {f.message}\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    service_factory: ServiceFactory | None = None,
    out: TextIO | None = None,
    err_out: TextIO | None = None,
) -> int:
    out, err_out = out or sys.stdout, err_out or sys.stderr
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    if args.area in ("worker", "changes", "costs"):
        try:
            return operations.run(args, out)
        except (ChangeError, ConfigurationError) as exc:
            err_out.write(f"error: {exc}\n")
            return 1
    try:
        if args.command == "validate-profile":
            validated = ProfileValidator().validate(
                parse_document(_read(args.file), _fmt(args.file, args.format))
            )
            _emit(
                out,
                args.json,
                {
                    "valid": True,
                    "sha256": validated.content_sha256,
                    "policy_version": validated.policy_version,
                },
                f"valid (sha256 {validated.content_sha256[:12]}, "
                f"policy v{validated.policy_version})",
            )
            return 0
        if args.command == "toolchains":
            policy = CommandPolicy()
            data = {
                "policy": policy.policy_id,
                "version": policy.version,
                "toolchains": policy.describe(),
                "capabilities": sorted(CAPABILITIES),
                "validation_policies": sorted(VALIDATION_POLICIES),
            }
            _emit(
                out,
                args.json,
                data,
                "\n".join(
                    [
                        f"policy {policy.policy_id} v{policy.version}",
                        *(f"  {f}: {', '.join(e)}" for f, e in policy.describe().items()),
                        "capabilities: " + ", ".join(sorted(CAPABILITIES)),
                    ]
                ),
            )
            return 0
        factory = service_factory or _default_services
        svc, resolver = factory()
        asyncio.run(_run(args, svc, resolver, out))
    except RegistryValidationError as exc:
        err_out.write(f"error: {exc.code}\n")
        _print_findings(exc, err_out)
        return 1
    except RegistryError as exc:
        err_out.write(f"error: {exc.code}: {exc}\n")
        return 1
    except RuntimeError as exc:
        err_out.write(f"error: {exc}\n")
        return 2
    return 0


def _default_services() -> Services:
    from ai_code_change_agent.composition import (
        build_git_provider_clients,
        build_github_auth,
        build_registry_service,
        build_repository_resolver,
    )
    from ai_code_change_agent.core.config import get_settings
    from ai_code_change_agent.persistence.session import create_engine, create_session_factory

    settings = get_settings()
    url = settings.database_url
    if url is None:
        msg = "DATABASE_URL is required"
        raise RuntimeError(msg)
    factory = create_session_factory(create_engine(url.get_secret_value()))
    return (
        build_registry_service(factory, build_git_provider_clients(build_github_auth(settings))),
        build_repository_resolver(factory),
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
