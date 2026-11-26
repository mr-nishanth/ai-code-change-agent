import copy
import json
from typing import Any

import pytest

from ai_code_change_agent.application.repositories.validation import (
    ProfileValidator,
    parse_document,
)
from ai_code_change_agent.domain.repositories.catalog import MAX_PROFILE_BYTES
from ai_code_change_agent.domain.repositories.errors import RegistryValidationError
from tests.support.registry_fixtures import FIXTURES, jvm_service, nestjs_backend, profile

V = ProfileValidator()


def codes(doc: object) -> set[str]:
    with pytest.raises(RegistryValidationError) as exc:
        V.validate(doc)
    return {f.code for f in exc.value.findings}


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_all_four_stack_fixtures_are_valid(name: str) -> None:
    out = V.validate(FIXTURES[name]())
    assert out.profile.repository_key == name and len(out.content_sha256) == 64
    assert out.policy_version == "1" and out.schema_version == 1


def test_jvm_fixture_valid_too() -> None:
    assert V.validate(jvm_service()).profile.toolchains == ("jvm",)


def test_hash_is_stable_and_order_independent() -> None:
    a = nestjs_backend()
    b = json.loads(json.dumps(a, sort_keys=True))
    assert V.validate(a).content_sha256 == V.validate(b).content_sha256
    changed = copy.deepcopy(a)
    changed["limits"] = {"max_files_changed": 7}
    assert V.validate(changed).content_sha256 != V.validate(a).content_sha256


def test_minimal_profile_is_valid() -> None:
    assert V.validate(profile("tiny")).profile.commands == ()


@pytest.mark.parametrize("doc", [None, [], "x", 5])
def test_document_must_be_an_object(doc: object) -> None:
    assert codes(doc) == {"not_an_object"}


def test_unknown_fields_are_rejected_everywhere() -> None:
    assert "extra_forbidden" in codes({**profile("xx"), "surprise": 1})
    doc = nestjs_backend()
    doc["commands"][0]["shell"] = "rm -rf /"
    assert "extra_forbidden" in codes(doc)
    doc = profile("xx", limits={"max_files_changed": 5, "root": True})
    assert "extra_forbidden" in codes(doc)


@pytest.mark.parametrize("bad", [{"schema_version": 2}, {"schema_version": "1"}])
def test_schema_version_is_pinned(bad: dict[str, Any]) -> None:
    assert codes({**profile("xx"), **bad})


def test_required_fields() -> None:
    assert "missing" in codes({"schema_version": 1})


def test_privilege_escalation_attempts_are_rejected() -> None:
    doc = nestjs_backend()
    assert "unknown_policy" in codes({**doc, "command_policy": "permissive"})
    assert "unknown_policy" in codes({**doc, "validation_policy": "none"})
    assert "unknown_capability" in codes(
        {**doc, "capabilities": [*doc["capabilities"], "root_shell"]}
    )
    d = copy.deepcopy(doc)
    d["commands"][1]["argv"] = ["bash", "-c", "npm"]
    assert "executable_not_allowed" in codes(d)
    d = copy.deepcopy(doc)
    d["commands"][1]["network"] = "package_registry"
    assert "network_not_allowed" in codes(d)
    d = copy.deepcopy(doc)
    d["commands"][1]["env"] = {"LD_PRELOAD": "evil.so"}
    assert "env_not_allowed" in codes(d)
    d = copy.deepcopy(doc)
    d["commands"][1]["toolchain"] = "shell"
    assert {"unknown_toolchain", "toolchain_not_declared"} <= codes(d)


def test_capability_must_be_selected_for_each_command_kind() -> None:
    doc = nestjs_backend()
    doc["capabilities"] = ["test_run"]
    assert "capability_not_granted" in codes(doc)


def test_toolchain_must_be_declared() -> None:
    doc = nestjs_backend()
    doc["toolchains"] = []
    assert "toolchain_not_declared" in codes(doc)
    assert "unknown_toolchain" in codes({**nestjs_backend(), "toolchains": ["node", "fortran"]})


def test_duplicate_ids_and_capabilities() -> None:
    doc = nestjs_backend()
    doc["commands"][1]["id"] = doc["commands"][0]["id"]
    assert "duplicate" in codes(doc)
    doc = nestjs_backend()
    doc["capabilities"] = ["test_run", "test_run", "build_run", "lint_run", "dependency_install"]
    assert "duplicate" in codes(doc)


@pytest.mark.parametrize(
    "limits",
    [
        {"max_files_changed": 501},
        {"max_files_changed": 0},
        {"max_diff_bytes": 6_000_000},
        {"max_runtime_seconds": 20_000},
    ],
)
def test_limits_are_bounded_by_platform_ceilings(limits: dict[str, int]) -> None:
    assert codes(profile("xx", limits=limits))


def test_indexing_globs_and_bounds() -> None:
    assert "glob_invalid" in codes(profile("xx", indexing={"include": ["../secrets/**"]}))
    assert "glob_invalid" in codes(profile("xx", indexing={"exclude": ["/etc/*"]}))
    assert "glob_invalid" in codes(profile("xx", indexing={"include": ["a b"]}))
    assert codes(profile("xx", indexing={"max_file_bytes": 10**9}))
    assert V.validate(
        profile("xx", indexing={"include": ["src/**"], "exclude": ["**/node_modules/**"]})
    )


def test_labels_and_tiers() -> None:
    assert "label_invalid" in codes(profile("xx", languages=["py thon"]))
    assert codes(profile("xx", execution_tier="huge"))
    assert V.validate(profile("xx", execution_tier="large")).profile.execution_tier == "large"


def test_key_format_and_command_field_bounds() -> None:
    assert codes(profile("Bad Key"))
    d = nestjs_backend()
    d["commands"][0]["argv"] = []
    assert codes(d)
    d = nestjs_backend()
    d["commands"][0]["argv"] = ["npm"] * 17
    assert codes(d)
    d = nestjs_backend()
    d["commands"][0]["timeout_seconds"] = 99999
    assert codes(d)
    d = nestjs_backend()
    d["commands"][0]["kind"] = "deploy"
    assert codes(d)


def test_every_finding_is_reported_not_just_the_first() -> None:
    doc = nestjs_backend()
    doc["command_policy"] = "x"
    doc["validation_policy"] = "y"
    doc["capabilities"] = ["nope"]
    assert len(codes(doc)) >= 3
    with pytest.raises(RegistryValidationError) as exc:
        V.validate(doc)
    assert len(exc.value.findings) >= 4


def test_findings_never_echo_input_values() -> None:
    marker = "SUPERSECRETVALUE123"
    with pytest.raises(RegistryValidationError) as exc:
        V.validate(
            {
                **profile("xx"),
                "languages": [marker + " bad"],
                "frameworks": 5,
                "execution_tier": marker,
            }
        )
    blob = " ".join(f"{f.field} {f.code} {f.message}" for f in exc.value.findings) + str(exc.value)
    assert marker not in blob


def test_parse_document_limits_and_errors() -> None:
    assert parse_document(b'{"a": 1}', "json") == {"a": 1}
    assert parse_document(b"a = 1", "toml") == {"a": 1}
    for raw, fmt in (
        (b"{nope", "json"),
        (b"a = ", "toml"),
        (b"\xff\xfe", "toml"),
        (b"[" * 100000, "json"),
    ):
        with pytest.raises(RegistryValidationError) as exc:
            parse_document(raw, fmt)  # type: ignore[arg-type]
        assert {f.code for f in exc.value.findings} == {"unparseable"}
    with pytest.raises(RegistryValidationError) as exc:
        parse_document(b" " * (MAX_PROFILE_BYTES + 1), "json")
    assert exc.value.findings[0].code == "too_large"
