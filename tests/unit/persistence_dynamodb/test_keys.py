"""Key builders: one per entity of DYNAMODB-DATA-MODEL.md section 4, with the documented shapes."""

import uuid
from datetime import UTC, datetime

import pytest

from ai_code_change_agent.persistence.dynamodb import keys
from ai_code_change_agent.persistence.dynamodb.keys import Key

RID = uuid.UUID("11111111-1111-4111-8111-111111111111")
AID = uuid.UUID("22222222-2222-4222-8222-222222222222")
REPO = uuid.UUID("33333333-3333-4333-8333-333333333333")
OTHER = uuid.UUID("44444444-4444-4444-8444-444444444444")
TS = datetime(2026, 11, 27, 9, 29, 17, 162000, tzinfo=UTC)
T = "2026-11-27T09:29:17.162000Z"
R, A, P, OID = str(RID), str(AID), str(REPO), str(OTHER)


@pytest.mark.parametrize(
    ("built", "pk", "sk"),
    [
        (keys.delivery("5b2c-webhook-17"), "DELIVERY#5b2c-webhook-17", "DELIVERY"),
        (keys.issue_guard("10042"), "JIRAISSUE#10042", "GUARD"),
        (keys.request(RID), f"REQ#{R}", "REQUEST"),
        (keys.attempt(RID, AID), f"REQ#{R}", f"ATT#{A}"),
        (keys.attempt_pointer(AID), f"ATTEMPT#{A}", "PTR"),
        (
            keys.agent_thread(AID, f"attempt:{A}:planning:9f3a1c2e"),
            f"ATTEMPT#{A}",
            f"THREAD#attempt:{A}:planning:9f3a1c2e",
        ),
        (keys.active_attempt_guard(RID, REPO), f"REQ#{R}", f"ACTIVE#{P}"),
        (keys.approval(RID, AID, TS, OTHER), f"REQ#{R}", f"APR#{A}#{T}#{OID}"),
        (keys.parent(RID), f"REQ#{R}", "PARENT"),
        (keys.parent_pointer(OTHER), f"PARENT#{OID}", "PTR"),
        (keys.child(RID, OTHER), f"REQ#{R}", f"CHILD#{OID}"),
        (keys.child_pointer(OTHER), f"CHILD#{OID}", "PTR"),
        (keys.child_attempt_link(AID), f"CHILDATT#{A}", "PTR"),
        (keys.repository(REPO), f"REPO#{P}", "REPO"),
        (keys.alias("service-a"), "ALIAS#service-a", "ALIAS"),
        (keys.alias_reverse(REPO, "service-a"), f"REPO#{P}", "ALIAS#service-a"),
        (keys.locator_guard("github:octo/service-a"), "LOCATOR#github:octo/service-a", "GUARD"),
        (
            keys.provider_identity_guard("github", "812345678"),
            "PROVIDERREPO#github#812345678",
            "GUARD",
        ),
        (keys.profile_version(REPO, 3), f"REPO#{P}", "PV#000003"),
        (keys.profile_version_pointer(OTHER), f"PROFILEVERSION#{OID}", "PTR"),
        (keys.project_mapping("SHOP", REPO), "PROJECT#SHOP", f"MAP#{P}"),
        (keys.audit_event("repository", P, TS, OTHER), f"AUDIT#repository#{P}", f"EV#{T}#{OID}"),
        (keys.audit_event("mapping", None, TS, OTHER), "AUDIT#mapping#-", f"EV#{T}#{OID}"),
        (keys.usage_attempt_copy(AID, TS, "run-1"), f"USAGE#ATT#{A}", f"{T}#run-1"),
        (keys.usage_session_copy("ses-9", TS, "run-1"), "USAGE#SES#ses-9", f"{T}#run-1"),
        (keys.llm_call_guard("run-1"), "LLMCALL#run-1", "GUARD"),
        (keys.provider_message_guard("msg_01"), "LLMMSG#msg_01", "GUARD"),
        (keys.job(OTHER), f"JOB#{OID}", "JOB"),
        (
            keys.job_dedupe_guard(f"code_change.child:{OID}:2"),
            f"JOBKEY#code_change.child:{OID}:2",
            "GUARD",
        ),
    ],
)
def test_entity_keys_match_the_data_model(built: Key, pk: str, sk: str) -> None:
    assert built == Key(pk, sk)
    assert built.as_dict() == {"PK": pk, "SK": sk}


def test_index_keys_match_the_data_model() -> None:
    assert keys.request_gsi1("SHOP-12", TS, RID) == {
        "GSI1PK": "ISSUEKEY#SHOP-12",
        "GSI1SK": f"REQ#{T}#{R}",
    }
    assert keys.issue_key_gsi1pk("SHOP-12") == "ISSUEKEY#SHOP-12"
    assert keys.attempt_gsi2(TS, AID) == {"GSI2PK": "LIST#ATTEMPT", "GSI2SK": f"{T}#{A}"}
    assert keys.repository_gsi2("service-a") == {"GSI2PK": "LIST#REPOSITORY", "GSI2SK": "service-a"}
    assert keys.mapping_gsi1(REPO, "SHOP") == {"GSI1PK": f"REPOMAPS#{P}", "GSI1SK": "SHOP"}
    assert keys.repository_mappings_gsi1pk(REPO) == f"REPOMAPS#{P}"
    assert keys.mapping_gsi2("SHOP", TS, OTHER) == {
        "GSI2PK": "LIST#MAPPING",
        "GSI2SK": f"SHOP#{T}#{OID}",
    }
    assert keys.audit_gsi2(TS, OTHER) == {"GSI2PK": "LIST#AUDIT", "GSI2SK": f"{T}#{OID}"}
    assert keys.job_dispatch_gsi3(TS, OTHER) == {"GSI3PK": "DISPATCH", "GSI3SK": f"{T}#{OID}"}
    assert keys.approval_prefix(AID) == f"APR#{A}#"
    assert keys.project_pk("SHOP") == "PROJECT#SHOP"
    assert keys.usage_attempt_pk(AID) == f"USAGE#ATT#{A}"
    assert keys.usage_session_pk("s") == "USAGE#SES#s"
    assert keys.audit_pk("repository", None) == "AUDIT#repository#-"


def test_uuids_are_lowercase_canonical() -> None:
    assert keys.request(R.upper()) == keys.request(RID)
    with pytest.raises(ValueError):
        keys.request("not-a-uuid")


def test_profile_versions_sort_as_strings() -> None:
    sks = [keys.profile_version(REPO, n).sk for n in (1, 2, 10, 100, 999_999)]
    assert sorted(sks) == sks
    for bad in (0, 1_000_000, True):
        with pytest.raises(ValueError):
            keys.profile_version(REPO, bad)


def test_components_are_checked() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        keys.alias("")
    with pytest.raises(ValueError, match="'#'"):
        keys.provider_identity_guard("git#hub", "1")
    with pytest.raises(ValueError, match="'#'"):
        keys.mapping_gsi2("SH#OP", TS, OTHER)
    with pytest.raises(ValueError, match="'#'"):
        keys.audit_event("repo#x", None, TS, OTHER)
    # a final component may contain the separator: an exact-match key stays unambiguous
    assert keys.delivery("a#b").pk == "DELIVERY#a#b"


def test_key_of_an_item() -> None:
    assert Key.of({"PK": "A", "SK": "B", "x": 1}) == Key("A", "B")
    with pytest.raises(ValueError):
        Key.of({"PK": "A"})
