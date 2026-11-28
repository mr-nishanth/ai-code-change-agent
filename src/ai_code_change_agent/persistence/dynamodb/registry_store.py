"""DynamoDB adapters for the registry ports (DYNAMODB-ACCESS-PATTERNS.md 5, R1-R20).

One `DynamoRegistryUnitOfWork` = one `TransactWriteItems` on commit (the `DynamoUnitOfWork`
pattern in `transactions.py`): reads are strongly consistent and remembered, writes are buffered
(read-your-writes), leaving the block without `commit()` discards everything. Error codes match
the SQL adapter: `repository_already_registered`, `repository_key_taken`, `alias_taken`,
`profile_version_conflict`, `publish_conflict`, `repository_state_conflict`; a lost optimistic
race is `ConcurrentModificationError`, which `RepositoryRegistryService` retries as a whole method.

Items (DYNAMODB-DATA-MODEL.md 4): `REPO#<id>/REPO` (row_version, `last_profile_version`
counter, GSI2 `LIST#REPOSITORY`), `ALIAS#<alias>/ALIAS` (global key/alias namespace, kind KEY
or ALIAS) with a reverse copy `REPO#<id>/ALIAS#<alias>`, guards `LOCATOR#<locator>/GUARD` and
`PROVIDERREPO#<provider>#<id>/GUARD`, profile versions `REPO#<id>/PV#<version:06d>` (immutable
except a single guarded `SET published_at`) with pointer `PROFILEVERSION#<vid>/PTR`, mappings
`PROJECT#<key>/MAP#<repo>` (row_version; GSI1 `REPOMAPS#<repo>`, GSI2 `LIST#MAPPING`) and audit
events `AUDIT#<type>#<id or ->/EV#<occurred_at>#<event_id>` (GSI2 `LIST#AUDIT`). Nothing is
ever deleted."""

import uuid
from typing import Any, Self

from ai_code_change_agent.domain.changes.errors import ConcurrentModificationError
from ai_code_change_agent.domain.repositories.errors import ConflictError
from ai_code_change_agent.domain.repositories.models import (
    AuditAction,
    AuditEvent,
    MappingStatus,
    NewRepository,
    ProfileVersion,
    ProjectMapping,
    Repository,
    RepositoryStatus,
)
from ai_code_change_agent.persistence.dynamodb import keys
from ai_code_change_agent.persistence.dynamodb.clock import Clock, SystemClock
from ai_code_change_agent.persistence.dynamodb.codec import (
    decode_enum,
    decode_int,
    decode_json,
    decode_timestamp,
    decode_uuid,
    encode_json,
    encode_timestamp,
)
from ai_code_change_agent.persistence.dynamodb.errors import CorruptItemError, Item
from ai_code_change_agent.persistence.dynamodb.table import Table
from ai_code_change_agent.persistence.dynamodb.transactions import DynamoUnitOfWork

GSI1 = "GSI1"
GSI2 = "GSI2"
_KIND_KEY = "KEY"
_KIND_ALIAS = "ALIAS"
_STATE_CONFLICT = "repository_state_conflict"


def _str(item: Item, name: str) -> str:
    value = item.get(name)
    if not isinstance(value, str):
        raise CorruptItemError(name)
    return value


def _opt_uuid(item: Item, name: str) -> uuid.UUID | None:
    return decode_uuid(item[name], name) if name in item else None


def _repo(item: Item) -> Repository:
    return Repository(
        id=decode_uuid(item.get("id"), "id"),
        repository_key=_str(item, "repository_key"),
        display_name=_str(item, "display_name"),
        git_provider=_str(item, "git_provider"),
        provider_repository_id=_str(item, "provider_repository_id"),
        owner=_str(item, "owner"),
        name=_str(item, "name"),
        canonical_locator=_str(item, "canonical_locator"),
        default_branch=_str(item, "default_branch"),
        status=decode_enum(RepositoryStatus, item.get("status"), "status"),
        access_reference=_str(item, "access_reference"),
        active_profile_version_id=_opt_uuid(item, "active_profile_version_id"),
        row_version=decode_int(item.get("row_version"), "row_version"),
        created_at=decode_timestamp(item.get("created_at"), "created_at"),
        updated_at=decode_timestamp(item.get("updated_at"), "updated_at"),
    )


def _version(item: Item) -> ProfileVersion:
    content = decode_json(item.get("content_json"), "content_json")
    if not isinstance(content, dict):
        raise CorruptItemError("content_json")
    published = item.get("published_at")
    return ProfileVersion(
        id=decode_uuid(item.get("id"), "id"),
        repository_id=decode_uuid(item.get("repository_id"), "repository_id"),
        version=decode_int(item.get("version"), "version"),
        schema_version=decode_int(item.get("schema_version"), "schema_version"),
        content=content,
        content_sha256=_str(item, "content_sha256"),
        policy_version=_str(item, "policy_version"),
        created_by=_str(item, "created_by"),
        created_at=decode_timestamp(item.get("created_at"), "created_at"),
        published_at=decode_timestamp(published, "published_at") if published is not None else None,
    )


def _mapping(item: Item) -> ProjectMapping:
    return ProjectMapping(
        id=decode_uuid(item.get("id"), "id"),
        jira_project_key=_str(item, "jira_project_key"),
        repository_id=decode_uuid(item.get("repository_id"), "repository_id"),
        status=decode_enum(MappingStatus, item.get("status"), "status"),
        created_by=_str(item, "created_by"),
        created_at=decode_timestamp(item.get("created_at"), "created_at"),
    )


def _event(item: Item) -> AuditEvent:
    metadata = decode_json(item.get("metadata_json"), "metadata_json")
    if not isinstance(metadata, dict):
        raise CorruptItemError("metadata_json")
    entity_id = item.get("entity_id")
    correlation_id = item.get("correlation_id")
    return AuditEvent(
        id=decode_uuid(item.get("id"), "id"),
        action=decode_enum(AuditAction, item.get("action"), "action"),
        entity_type=_str(item, "entity_type"),
        entity_id=decode_uuid(entity_id, "entity_id") if entity_id is not None else None,
        actor=_str(item, "actor"),
        metadata=metadata,
        correlation_id=correlation_id if isinstance(correlation_id, str) else None,
        occurred_at=decode_timestamp(item.get("occurred_at"), "occurred_at"),
    )


def _key_collision(old: Item | None) -> BaseException:
    """ALIAS#<key> exists: the key of a registered repository, or an alias of another one."""
    if old is not None and old.get("kind") == _KIND_ALIAS:
        return ConflictError("repository_key_taken")
    return ConflictError("repository_already_registered")


def _state_conflict(target: RepositoryStatus) -> Any:
    def failure(old: Item | None) -> BaseException:
        # ACTIVE requires an active profile version; anything else is a lost race
        if target is RepositoryStatus.ACTIVE and (
            old is not None and "active_profile_version_id" not in old
        ):
            return ConflictError(_STATE_CONFLICT)
        return ConcurrentModificationError()

    return failure


class DynamoRegistryStore:
    def __init__(self, uow: "DynamoRegistryUnitOfWork") -> None:
        self._u = uow

    def _now(self) -> str:
        return encode_timestamp(self._u.clock.now())

    # R1 ----------------------------------------------------------------------------------
    async def add_repository(self, new: NewRepository, *, created_by: str) -> Repository:
        repository_id = uuid.uuid4()
        now = self._now()
        key = keys.repository(repository_id)
        repo = self._u.create(
            {
                **key.as_dict(),
                **keys.repository_gsi2(new.repository_key),
                "entity": "repository",
                "id": str(repository_id),
                "repository_key": new.repository_key,
                "display_name": new.display_name,
                "git_provider": new.git_provider,
                "provider_repository_id": new.provider_repository_id,
                "owner": new.owner,
                "name": new.name,
                "canonical_locator": new.canonical_locator,
                "default_branch": new.default_branch,
                "status": RepositoryStatus.DISABLED.value,
                "access_reference": new.access_reference,
                "last_profile_version": 0,
                "row_version": 1,
                "created_by": created_by,
                "created_at": now,
                "updated_at": now,
            },
            on_exists=ConcurrentModificationError(),
        )
        guard = {"repository_id": str(repository_id), "created_at": now}
        already = ConflictError("repository_already_registered")
        # Order matters: the first failing item names the error, as the SQL flush order did.
        self._u.create(
            {**keys.locator_guard(new.canonical_locator).as_dict(), "entity": "locator", **guard},
            on_exists=already,
        )
        provider_guard = keys.provider_identity_guard(new.git_provider, new.provider_repository_id)
        self._u.create(
            {**provider_guard.as_dict(), "entity": "provider_identity", **guard}, on_exists=already
        )
        self._alias(repository_id, new.repository_key, _KIND_KEY, created_by, now, _key_collision)
        return _repo(repo)

    def _alias(
        self,
        repository_id: uuid.UUID,
        alias: str,
        kind: str,
        created_by: str,
        now: str,
        on_exists: Any,
    ) -> None:
        body = {
            "entity": "alias",
            "alias": alias,
            "kind": kind,
            "repository_id": str(repository_id),
            "created_by": created_by,
            "created_at": now,
        }
        self._u.create({**keys.alias(alias).as_dict(), **body}, on_exists=on_exists)
        self._u.create(
            {**keys.alias_reverse(repository_id, alias).as_dict(), **body}, on_exists=on_exists
        )

    # R2-R5, R8-R10 -----------------------------------------------------------------------
    async def _by_key(self, key: str, *, depend: bool) -> Repository | None:
        alias = await self._u.get(keys.alias(key), depend=depend)
        if alias is None or alias.get("kind") != _KIND_KEY:
            return None
        item = await self._u.get(
            keys.repository(decode_uuid(alias.get("repository_id"), "repository_id")),
            depend=depend,
        )
        return _repo(item) if item is not None else None

    async def get_repository(self, key: str) -> Repository | None:
        return await self._by_key(key, depend=False)

    async def lock_repository(self, key: str) -> Repository | None:
        # the commit carries row_version = :seen on REPO (written) or a ConditionCheck (read only)
        return await self._by_key(key, depend=True)

    async def get_repository_by_id(self, repository_id: uuid.UUID) -> Repository | None:
        item = await self._u.get(keys.repository(repository_id), depend=False)
        return _repo(item) if item is not None else None

    async def list_repositories(self) -> list[Repository]:
        """R5, display only: GSI2 `LIST#REPOSITORY` keys, then the items (eventual)."""
        rows = await self._u.table.query(
            key_condition="GSI2PK = :pk",
            values={":pk": keys.GSI_LIST_REPOSITORY},
            index=GSI2,
        )
        return [_repo(item) for item in await self._u.table.fetch(rows, consistent=False)]

    async def _bump(self, repository_id: uuid.UUID, changes: Item, **kwargs: Any) -> Repository:
        key = keys.repository(repository_id)
        item = await self._u.get(key)
        if item is None:
            raise ConflictError(_STATE_CONFLICT)
        item.update(changes)
        item["updated_at"] = self._now()
        return _repo(self._u.replace(item, **kwargs))

    async def set_status(self, repository_id: uuid.UUID, status: RepositoryStatus) -> Repository:
        item = await self._u.get(keys.repository(repository_id))
        if status is RepositoryStatus.ACTIVE:
            if item is not None and "active_profile_version_id" not in item:
                raise ConflictError(_STATE_CONFLICT)
            return await self._bump(
                repository_id,
                {"status": status.value},
                condition="attribute_exists(active_profile_version_id)",
                on_failed=_state_conflict(status),
            )
        return await self._bump(repository_id, {"status": status.value})

    # R7 ----------------------------------------------------------------------------------
    async def add_alias(self, repository_id: uuid.UUID, alias: str, created_by: str) -> None:
        await self._u.get(keys.repository(repository_id))  # REPO ConditionCheck on commit
        self._alias(
            repository_id, alias, _KIND_ALIAS, created_by, self._now(), ConflictError("alias_taken")
        )

    async def list_aliases(self, repository_id: uuid.UUID) -> list[str]:
        rows = await self._u.query(keys.repository_pk(repository_id), keys.SK_ALIAS)
        return [_str(row, "alias") for row in rows]

    async def find_by_alias(self, alias: str) -> Repository | None:
        item = await self._u.get(keys.alias(alias), depend=False)
        if item is None:
            return None
        return await self.get_repository_by_id(
            decode_uuid(item.get("repository_id"), "repository_id")
        )

    async def find_by_locator(self, locator: str) -> Repository | None:
        item = await self._u.get(keys.locator_guard(locator), depend=False)
        if item is None:
            return None
        return await self.get_repository_by_id(
            decode_uuid(item.get("repository_id"), "repository_id")
        )

    # R11-R15 -----------------------------------------------------------------------------
    async def add_profile_version(
        self,
        repository_id: uuid.UUID,
        *,
        content: dict[str, Any],
        content_sha256: str,
        schema_version: int,
        policy_version: str,
        created_by: str,
    ) -> ProfileVersion:
        repo = await self._u.get(keys.repository(repository_id))
        if repo is None:
            raise ConflictError("profile_version_conflict")
        number = decode_int(repo.get("last_profile_version", 0), "last_profile_version") + 1
        # the REPO counter allocates the number under row_version = :seen (first item: a lost race
        # is ConcurrentModificationError, retried by the service)
        await self._bump(repository_id, {"last_profile_version": number})
        version_id = uuid.uuid4()
        item = self._u.create(
            {
                **keys.profile_version(repository_id, number).as_dict(),
                "entity": "profile_version",
                "id": str(version_id),
                "repository_id": str(repository_id),
                "version": number,
                "schema_version": schema_version,
                "content_json": encode_json(content),
                "content_sha256": content_sha256,
                "policy_version": policy_version,
                "created_by": created_by,
                "created_at": self._now(),
            },
            on_exists=ConflictError("profile_version_conflict"),
        )
        self._u.create(
            {
                **keys.profile_version_pointer(version_id).as_dict(),
                "entity": "profile_version_pointer",
                "repository_id": str(repository_id),
                "version": number,
            },
            on_exists=ConflictError("profile_version_conflict"),
        )
        return _version(item)

    async def get_profile_version(
        self, repository_id: uuid.UUID, version: int
    ) -> ProfileVersion | None:
        if isinstance(version, bool) or not 1 <= version <= 999_999:
            return None
        item = await self._u.get(keys.profile_version(repository_id, version))
        return _version(item) if item is not None else None

    async def _pv_key(self, version_id: uuid.UUID) -> keys.Key | None:
        pointer = await self._u.get(keys.profile_version_pointer(version_id), depend=False)
        if pointer is None:
            return None
        return keys.profile_version(
            decode_uuid(pointer.get("repository_id"), "repository_id"),
            decode_int(pointer.get("version"), "version"),
        )

    async def get_profile_version_by_id(self, version_id: uuid.UUID) -> ProfileVersion | None:
        key = await self._pv_key(version_id)
        if key is None:
            return None
        item = await self._u.get(key, depend=False)
        return _version(item) if item is not None else None

    async def list_profile_versions(self, repository_id: uuid.UUID) -> list[ProfileVersion]:
        rows = await self._u.query(keys.repository_pk(repository_id), keys.SK_PROFILE_VERSION)
        return [_version(row) for row in rows]

    async def mark_published(self, version_id: uuid.UUID) -> ProfileVersion:
        key = await self._pv_key(version_id)
        item = await self._u.get(key) if key is not None else None
        if key is None or item is None or "published_at" in item:
            raise ConflictError("publish_conflict")
        now = self._now()
        # published versions are immutable: the only update on a PV item, guarded by its condition;
        # content attributes appear in no update expression
        image = self._u.update(
            key,
            image={**item, "published_at": now},
            expression="SET published_at = :now",
            condition="attribute_exists(PK) AND attribute_not_exists(published_at)",
            values={":now": now},
        )
        return _version(image)

    async def set_active_version(
        self, repository_id: uuid.UUID, version_id: uuid.UUID
    ) -> Repository:
        key = await self._pv_key(version_id)
        item = await self._u.get(key, depend=False) if key is not None else None
        if (
            key is None
            or item is None
            or item.get("repository_id") != str(repository_id)
            or "published_at" not in item
        ):
            # a draft or another repository's version (the SQL foreign key / trigger)
            raise ConflictError(_STATE_CONFLICT)
        if not self._u.is_written(key):  # published in this unit of work: its update guards it
            self._u.check(
                key,
                condition="attribute_exists(published_at) AND repository_id = :rid",
                values={":rid": str(repository_id)},
                on_failed=ConflictError(_STATE_CONFLICT),
            )
        return await self._bump(repository_id, {"active_profile_version_id": str(version_id)})

    # R16-R17 -----------------------------------------------------------------------------
    async def set_mapping(
        self,
        jira_project_key: str,
        repository_id: uuid.UUID,
        status: MappingStatus,
        created_by: str,
    ) -> tuple[ProjectMapping, bool]:
        await self._u.get(keys.repository(repository_id))  # REPO ConditionCheck on commit
        key = keys.project_mapping(jira_project_key, repository_id)
        item = await self._u.get(key)
        now_dt = self._u.clock.now()
        now = encode_timestamp(now_dt)
        if item is None:
            mapping_id = uuid.uuid4()
            created = self._u.create(
                {
                    **key.as_dict(),
                    **keys.mapping_gsi1(repository_id, jira_project_key),
                    **keys.mapping_gsi2(jira_project_key, now_dt, mapping_id),
                    "entity": "project_mapping",
                    "id": str(mapping_id),
                    "jira_project_key": jira_project_key,
                    "repository_id": str(repository_id),
                    "status": status.value,
                    "row_version": 1,
                    "created_by": created_by,
                    "created_at": now,
                    "updated_at": now,
                },
                on_exists=ConcurrentModificationError(),
            )
            return _mapping(created), True
        item.update({"status": status.value, "updated_at": now})
        return _mapping(self._u.replace(item)), False

    async def list_mappings(
        self,
        *,
        jira_project_key: str | None = None,
        repository_id: uuid.UUID | None = None,
        status: MappingStatus | None = None,
    ) -> list[ProjectMapping]:
        if jira_project_key is not None and repository_id is not None:
            # authorisation: one consistent read of the unique pair
            item = await self._u.get(keys.project_mapping(jira_project_key, repository_id))
            rows = [item] if item is not None else []
        elif jira_project_key is not None:
            rows = await self._u.query(keys.project_pk(jira_project_key), keys.SK_MAPPING)
        elif repository_id is not None:  # display only (eventual GSI1 keys, eventual items)
            index_rows = await self._u.table.query(
                key_condition="GSI1PK = :pk",
                values={":pk": keys.repository_mappings_gsi1pk(repository_id)},
                index=GSI1,
            )
            rows = await self._u.table.fetch(index_rows, consistent=False)
        else:  # display only (eventual GSI2 keys and items, sorted by project, created_at)
            index_rows = await self._u.table.query(
                key_condition="GSI2PK = :pk", values={":pk": keys.GSI_LIST_MAPPING}, index=GSI2
            )
            rows = await self._u.table.fetch(index_rows, consistent=False)
        mappings = sorted(
            (_mapping(row) for row in rows), key=lambda m: (m.jira_project_key, m.created_at)
        )
        return [m for m in mappings if status is None or m.status is status]


class DynamoAuditLog:
    def __init__(self, uow: "DynamoRegistryUnitOfWork") -> None:
        self._u = uow

    async def record(self, event: AuditEvent) -> None:
        """R18: an append-only item in the mutation's own transaction."""
        occurred = event.occurred_at or self._u.clock.now()
        entity_id = str(event.entity_id) if event.entity_id is not None else None
        item: Item = {
            **keys.audit_event(event.entity_type, entity_id, occurred, event.id).as_dict(),
            **keys.audit_gsi2(occurred, event.id),
            "entity": "audit_event",
            "id": str(event.id),
            "action": event.action.value,
            "entity_type": event.entity_type,
            "actor": event.actor,
            "metadata_json": encode_json(dict(event.metadata)),
            "occurred_at": encode_timestamp(occurred),
        }
        if entity_id is not None:
            item["entity_id"] = entity_id
        if event.correlation_id is not None:
            item["correlation_id"] = event.correlation_id
        self._u.create(item, on_exists=ConflictError("audit_event_conflict"))

    async def list_events(
        self, *, entity_type: str | None = None, entity_id: uuid.UUID | None = None, limit: int = 50
    ) -> list[AuditEvent]:
        """R19 (display only, eventually consistent): newest first. Both filters: the entity's
        partition; otherwise the GSI2 listing, filtered by whichever one is given on the index
        (GSI2 projects `entity_type` and `entity_id`), then the matching events are fetched."""
        if limit < 1:
            return []
        if entity_type is not None and entity_id is not None:
            rows = await self._u.table.query(
                key_condition="PK = :pk AND begins_with(SK, :ev)",
                values={
                    ":pk": keys.audit_pk(entity_type, str(entity_id)),
                    ":ev": keys.SK_AUDIT_EVENT,
                },
                consistent=False,
                forward=False,
                limit=limit,
            )
        else:
            filters: list[str] = []
            values: dict[str, Any] = {":pk": keys.GSI_LIST_AUDIT}
            if entity_type is not None:
                filters.append("entity_type = :type")
                values[":type"] = entity_type
            if entity_id is not None:
                filters.append("entity_id = :id")
                values[":id"] = str(entity_id)
            rows = await self._u.table.query(
                key_condition="GSI2PK = :pk",
                values=values,
                index=GSI2,
                filter_expression=" AND ".join(filters) or None,
                forward=False,
                limit=limit,
            )
            rows = await self._u.table.fetch(rows, consistent=False)
        return [_event(row) for row in rows]


class DynamoRegistryUnitOfWork(DynamoUnitOfWork):
    """One transaction. Leaving the block without commit() discards everything."""

    def __init__(self, table: Table, clock: Clock | None = None) -> None:
        super().__init__(table)
        self.clock: Clock = clock or SystemClock()
        self._store = DynamoRegistryStore(self)
        self._audit = DynamoAuditLog(self)

    @property
    def store(self) -> DynamoRegistryStore:
        return self._store

    @property
    def audit(self) -> DynamoAuditLog:
        return self._audit

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        return self
