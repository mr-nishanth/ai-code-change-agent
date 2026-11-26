"""Deterministic repository resolution. No LLM, no network, no defaults, no fallbacks.

A Story may name a repository by registry alias or in a provider's reference syntax (for example
`owner/name` or the provider's web URL); provider parsers turn the latter into the registry's
canonical locator. The reference only *names* a repository: it must still be registered, ACTIVE,
mapped to the Story's Jira project and have a valid published profile. Nothing is fetched.
"""

import re
from collections.abc import Sequence

from ai_code_change_agent.application.jira.models import JiraIssue
from ai_code_change_agent.application.repositories.ports import (
    RegistryStore,
    RegistryUnitOfWork,
    RepositoryReferenceParser,
)
from ai_code_change_agent.application.repositories.service import UowFactory
from ai_code_change_agent.application.repositories.validation import ProfileValidator
from ai_code_change_agent.domain.repositories.errors import RegistryValidationError
from ai_code_change_agent.domain.repositories.models import (
    PROJECT_KEY_RE,
    REPOSITORY_KEY_RE,
    MappingStatus,
    Repository,
    RepositoryStatus,
)
from ai_code_change_agent.domain.repositories.resolution import (
    ResolutionOutcome,
    ResolutionRequest,
    ResolutionResult,
)

# An explicit marker in the Story summary or description, e.g. "Repository: shop-api" or
# "Repo: owner/name". Free-form mentions without the marker are never read. A marker only NAMES a
# repository; the resolver still requires registration, ACTIVE status, an active mapping to the
# Story's project and a valid profile, and every Git host operation re-checks the provider's access.
_MARKER = re.compile(
    r"(?<![A-Za-z0-9_])(?:repository|repo)[ \t]*:[ \t]*([A-Za-z0-9][A-Za-z0-9_.:/-]{0,199})",
    re.IGNORECASE,
)
_MAX_MARKERS = 10


def _normalise(reference: str) -> str:
    """Bare names follow the registry key syntax (`shop_api` names `shop-api`); provider
    references (owner/name, URLs) are left to the provider parsers."""
    value = reference.rstrip(".,:;/-")
    return value if "/" in value else value.lower().replace("_", "-")


def story_references(issue: JiraIssue) -> tuple[str, ...]:
    """Distinct references named by explicit markers in the summary and description."""
    texts = (issue.summary or "", issue.description.text if issue.description else "")
    found: list[str] = []
    for text in texts:
        for match in _MARKER.finditer(text):
            value = _normalise(match.group(1))
            if value and value not in found:
                found.append(value)
            if len(found) > _MAX_MARKERS:
                return tuple(found)
    return tuple(found)


def build_resolution_request(issue: JiraIssue, reference_field: str | None) -> ResolutionRequest:
    """References from the configured logical field (plain text only) and from explicit
    `Repository:` markers in the Story. Several distinct references resolve as AMBIGUOUS."""
    values: tuple[str, ...] = ()
    field = issue.custom_fields.get(reference_field) if reference_field else None
    if field is not None:
        if field.kind in ("text", "option") and field.text:
            values = (field.text,)
        elif field.kind == "options":
            values = field.values
    values = (*values, *story_references(issue))
    return ResolutionRequest(
        issue_key=issue.key,
        project_key=issue.project.key if issue.project and issue.project.key else "",
        references=tuple(v for v in values if v.strip()),
    )


def _fail(outcome: ResolutionOutcome, detail: str) -> ResolutionResult:
    return ResolutionResult(outcome=outcome, detail=detail)


class RepositoryResolver:
    def __init__(
        self,
        uow_factory: UowFactory,
        validator: ProfileValidator | None = None,
        parsers: Sequence[RepositoryReferenceParser] = (),
    ) -> None:
        self._uow = uow_factory
        self._validator = validator or ProfileValidator()
        self._parsers = tuple(parsers)

    async def resolve(self, request: ResolutionRequest) -> ResolutionResult:
        if not PROJECT_KEY_RE.fullmatch(request.project_key):
            return _fail(ResolutionOutcome.PROJECT_NOT_AUTHORISED, "invalid_project_key")
        async with self._uow() as uow:
            store = uow.store
            candidate = await self._candidate(store, request)
            if isinstance(candidate, ResolutionResult):
                return candidate
            return await self._authorise(uow, candidate, request.project_key)

    async def _candidate(
        self, store: RegistryStore, request: ResolutionRequest
    ) -> Repository | ResolutionResult:
        references = {r.strip().lower() for r in request.references if r.strip()}
        if len(references) > 1:
            return _fail(ResolutionOutcome.AMBIGUOUS_REFERENCE, "multiple_references")
        if references:
            (reference,) = references
            if REPOSITORY_KEY_RE.fullmatch(reference):
                repo = await store.find_by_alias(reference)
            else:
                # Provider syntax (owner/name, web URL) becomes a locator string for a registry
                # lookup; anything no parser accepts never reaches a lookup.
                locators = {loc for p in self._parsers if (loc := p.locator(reference))}
                if len(locators) != 1:
                    return _fail(ResolutionOutcome.UNKNOWN_REPOSITORY, "reference_unsupported")
                repo = await store.find_by_locator(locators.pop())
            if repo is None:
                return _fail(ResolutionOutcome.UNKNOWN_REPOSITORY, "reference_not_registered")
            return repo
        mappings = await store.list_mappings(
            jira_project_key=request.project_key, status=MappingStatus.ACTIVE
        )
        if not mappings:
            return _fail(ResolutionOutcome.MISSING_REFERENCE, "no_reference_and_no_project_mapping")
        if len(mappings) > 1:
            return _fail(ResolutionOutcome.AMBIGUOUS_REFERENCE, "multiple_project_mappings")
        repo = await store.get_repository_by_id(mappings[0].repository_id)
        if repo is None:
            return _fail(ResolutionOutcome.UNKNOWN_REPOSITORY, "mapped_repository_missing")
        return repo

    async def _authorise(
        self, uow: RegistryUnitOfWork, repo: Repository, project_key: str
    ) -> ResolutionResult:
        store = uow.store
        if repo.status is not RepositoryStatus.ACTIVE:
            return _fail(ResolutionOutcome.DISABLED_REPOSITORY, "repository_disabled")
        mappings = await store.list_mappings(
            jira_project_key=project_key, repository_id=repo.id, status=MappingStatus.ACTIVE
        )
        if not mappings:
            return _fail(ResolutionOutcome.PROJECT_NOT_AUTHORISED, "no_active_project_mapping")
        if repo.active_profile_version_id is None:
            return _fail(ResolutionOutcome.PROFILE_MISSING, "no_active_profile_version")
        version = await store.get_profile_version_by_id(repo.active_profile_version_id)
        if version is None or version.repository_id != repo.id:
            return _fail(ResolutionOutcome.PROFILE_INVALID, "active_version_not_found")
        if not version.published:
            return _fail(ResolutionOutcome.PROFILE_INVALID, "active_version_not_published")
        try:
            validated = self._validator.validate(version.content)
        except RegistryValidationError as exc:
            codes = ",".join(sorted({f.code for f in exc.findings})[:5])
            return _fail(ResolutionOutcome.PROFILE_INVALID, f"revalidation_failed:{codes}")
        if validated.profile.repository_key != repo.repository_key:
            return _fail(ResolutionOutcome.PROFILE_INVALID, "profile_key_mismatch")
        return ResolutionResult(
            outcome=ResolutionOutcome.RESOLVED,
            detail="resolved",
            repository_id=repo.id,
            repository_key=repo.repository_key,
            profile_version_id=version.id,
            profile_version=version.version,
            profile_sha256=version.content_sha256,
        )
