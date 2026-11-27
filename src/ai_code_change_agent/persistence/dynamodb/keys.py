"""Key builders for every entity of the application table (DYNAMODB-DATA-MODEL.md section 4).

Each builder returns a `Key` (`PK`, `SK`); index builders return the `GSI<n>PK`/`GSI<n>SK`
attributes to merge into an item (sparse indexes: only items that belong in an index carry its
keys). UUIDs are normalised to lowercase canonical strings and timestamps use the codec's
fixed-width UTC form, so keys sort correctly as strings. Components must be non-empty; a component
that is followed by more key material in the same attribute must not contain `#`, so prefixes
(`begins_with`) stay unambiguous."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from ai_code_change_agent.persistence.dynamodb.codec import encode_timestamp, encode_uuid

SEP = "#"
Id = uuid.UUID | str

# Sort-key prefixes inside the change aggregate (PK = REQ#<rid>) and the repository collection.
SK_REQUEST = "REQUEST"
SK_ATTEMPT = "ATT#"
SK_ACTIVE = "ACTIVE#"
SK_APPROVAL = "APR#"
SK_PARENT = "PARENT"
SK_CHILD = "CHILD#"
SK_REPOSITORY = "REPO"
SK_ALIAS = "ALIAS#"
SK_PROFILE_VERSION = "PV#"
SK_MAPPING = "MAP#"
SK_AUDIT_EVENT = "EV#"
SK_THREAD = "THREAD#"
SK_GUARD = "GUARD"
SK_POINTER = "PTR"

GSI_LIST_ATTEMPT = "LIST#ATTEMPT"
GSI_LIST_REPOSITORY = "LIST#REPOSITORY"
GSI_LIST_MAPPING = "LIST#MAPPING"
GSI_LIST_AUDIT = "LIST#AUDIT"
GSI_DISPATCH = "DISPATCH"


@dataclass(frozen=True, order=True)
class Key:
    pk: str
    sk: str

    def as_dict(self) -> dict[str, str]:
        return {"PK": self.pk, "SK": self.sk}

    @classmethod
    def of(cls, item: dict[str, object]) -> "Key":
        pk, sk = item.get("PK"), item.get("SK")
        if not isinstance(pk, str) or not isinstance(sk, str):
            raise ValueError("item has no string PK/SK")
        return cls(pk, sk)


def _last(value: str) -> str:
    """A final component: anything non-empty."""
    if not isinstance(value, str) or not value:
        raise ValueError("key component must be a non-empty string")
    return value


def _inner(value: str) -> str:
    """A component followed by more key material: non-empty and without the separator."""
    if SEP in _last(value):
        raise ValueError("key component must not contain '#'")
    return value


def _id(value: Id) -> str:
    return encode_uuid(value)


def _ts(value: datetime) -> str:
    return encode_timestamp(value)


def _join(*parts: str) -> str:
    return SEP.join(parts)


# Webhook intake


def delivery(delivery_id: str) -> Key:
    return Key(_join("DELIVERY", _last(delivery_id)), "DELIVERY")


def issue_guard(jira_issue_id: str) -> Key:
    return Key(_join("JIRAISSUE", _last(jira_issue_id)), SK_GUARD)


# Change aggregate


def request_pk(request_id: Id) -> str:
    return _join("REQ", _id(request_id))


def request(request_id: Id) -> Key:
    return Key(request_pk(request_id), SK_REQUEST)


def request_gsi1(jira_issue_key: str, created_at: datetime, request_id: Id) -> dict[str, str]:
    return {
        "GSI1PK": _join("ISSUEKEY", _last(jira_issue_key)),
        "GSI1SK": _join("REQ", _ts(created_at), _id(request_id)),
    }


def issue_key_gsi1pk(jira_issue_key: str) -> str:
    return _join("ISSUEKEY", _last(jira_issue_key))


def attempt(request_id: Id, attempt_id: Id) -> Key:
    return Key(request_pk(request_id), SK_ATTEMPT + _id(attempt_id))


def attempt_gsi2(created_at: datetime, attempt_id: Id) -> dict[str, str]:
    return {"GSI2PK": GSI_LIST_ATTEMPT, "GSI2SK": _join(_ts(created_at), _id(attempt_id))}


def attempt_pk(attempt_id: Id) -> str:
    return _join("ATTEMPT", _id(attempt_id))


def attempt_pointer(attempt_id: Id) -> Key:
    return Key(attempt_pk(attempt_id), SK_POINTER)


def agent_thread(attempt_id: Id, thread_id: str) -> Key:
    return Key(attempt_pk(attempt_id), SK_THREAD + _last(thread_id))


def active_attempt_guard(request_id: Id, repository_id: Id) -> Key:
    return Key(request_pk(request_id), SK_ACTIVE + _id(repository_id))


def approval(request_id: Id, attempt_id: Id, decided_at: datetime, approval_id: Id) -> Key:
    sk = SK_APPROVAL + _join(_id(attempt_id), _ts(decided_at), _id(approval_id))
    return Key(request_pk(request_id), sk)


def approval_prefix(attempt_id: Id) -> str:
    return SK_APPROVAL + _id(attempt_id) + SEP


def parent(request_id: Id) -> Key:
    return Key(request_pk(request_id), SK_PARENT)


def parent_pointer(parent_id: Id) -> Key:
    return Key(_join("PARENT", _id(parent_id)), SK_POINTER)


def child(request_id: Id, child_id: Id) -> Key:
    return Key(request_pk(request_id), SK_CHILD + _id(child_id))


def child_pointer(child_id: Id) -> Key:
    return Key(_join("CHILD", _id(child_id)), SK_POINTER)


def child_attempt_link(attempt_id: Id) -> Key:
    return Key(_join("CHILDATT", _id(attempt_id)), SK_POINTER)


# Repository registry


def repository_pk(repository_id: Id) -> str:
    return _join("REPO", _id(repository_id))


def repository(repository_id: Id) -> Key:
    return Key(repository_pk(repository_id), SK_REPOSITORY)


def repository_gsi2(repository_key: str) -> dict[str, str]:
    return {"GSI2PK": GSI_LIST_REPOSITORY, "GSI2SK": _last(repository_key)}


def alias(alias_name: str) -> Key:
    return Key(_join("ALIAS", _last(alias_name)), "ALIAS")


def alias_reverse(repository_id: Id, alias_name: str) -> Key:
    return Key(repository_pk(repository_id), SK_ALIAS + _last(alias_name))


def locator_guard(canonical_locator: str) -> Key:
    return Key(_join("LOCATOR", _last(canonical_locator)), SK_GUARD)


def provider_identity_guard(git_provider: str, provider_repository_id: str) -> Key:
    pk = _join("PROVIDERREPO", _inner(git_provider), _last(provider_repository_id))
    return Key(pk, SK_GUARD)


def profile_version(repository_id: Id, version: int) -> Key:
    if isinstance(version, bool) or not 1 <= version <= 999_999:
        raise ValueError("profile version must be 1..999999")
    return Key(repository_pk(repository_id), f"{SK_PROFILE_VERSION}{version:06d}")


def profile_version_pointer(version_id: Id) -> Key:
    return Key(_join("PROFILEVERSION", _id(version_id)), SK_POINTER)


def project_pk(jira_project_key: str) -> str:
    return _join("PROJECT", _last(jira_project_key))


def project_mapping(jira_project_key: str, repository_id: Id) -> Key:
    return Key(project_pk(jira_project_key), SK_MAPPING + _id(repository_id))


def mapping_gsi1(repository_id: Id, jira_project_key: str) -> dict[str, str]:
    return {"GSI1PK": _join("REPOMAPS", _id(repository_id)), "GSI1SK": _last(jira_project_key)}


def repository_mappings_gsi1pk(repository_id: Id) -> str:
    return _join("REPOMAPS", _id(repository_id))


def mapping_gsi2(jira_project_key: str, created_at: datetime, mapping_id: Id) -> dict[str, str]:
    sk = _join(_inner(jira_project_key), _ts(created_at), _id(mapping_id))
    return {"GSI2PK": GSI_LIST_MAPPING, "GSI2SK": sk}


def audit_pk(entity_type: str, entity_id: str | None) -> str:
    return _join("AUDIT", _inner(entity_type), _last(entity_id) if entity_id is not None else "-")


def audit_event(
    entity_type: str, entity_id: str | None, occurred_at: datetime, event_id: Id
) -> Key:
    sk = SK_AUDIT_EVENT + _join(_ts(occurred_at), _id(event_id))
    return Key(audit_pk(entity_type, entity_id), sk)


def audit_gsi2(occurred_at: datetime, event_id: Id) -> dict[str, str]:
    return {"GSI2PK": GSI_LIST_AUDIT, "GSI2SK": _join(_ts(occurred_at), _id(event_id))}


# LLM usage ledger


def usage_attempt_pk(attempt_id: Id) -> str:
    return _join("USAGE", "ATT", _id(attempt_id))


def usage_session_pk(session_id: str) -> str:
    return _join("USAGE", "SES", _last(session_id))


def usage_attempt_copy(attempt_id: Id, created_at: datetime, call_id: str) -> Key:
    return Key(usage_attempt_pk(attempt_id), _join(_ts(created_at), _last(call_id)))


def usage_session_copy(session_id: str, created_at: datetime, call_id: str) -> Key:
    return Key(usage_session_pk(session_id), _join(_ts(created_at), _last(call_id)))


def llm_call_guard(call_id: str) -> Key:
    return Key(_join("LLMCALL", _last(call_id)), SK_GUARD)


def provider_message_guard(provider_message_id: str) -> Key:
    return Key(_join("LLMMSG", _last(provider_message_id)), SK_GUARD)


# Jobs and the outbox


def job(job_id: Id) -> Key:
    return Key(_join("JOB", _id(job_id)), "JOB")


def job_dispatch_gsi3(due_at: datetime, job_id: Id) -> dict[str, str]:
    return {"GSI3PK": GSI_DISPATCH, "GSI3SK": _join(_ts(due_at), _id(job_id))}


def job_dedupe_guard(dedupe_key: str) -> Key:
    return Key(_join("JOBKEY", _last(dedupe_key)), SK_GUARD)
