"""Central redaction, per-kind policy and limits in front of any store."""

import uuid
from pathlib import Path

import pytest

from ai_code_change_agent.application.artifacts.errors import (
    ArtifactRedactionError,
    ArtifactSecretError,
    ArtifactTooLargeError,
)
from ai_code_change_agent.application.artifacts.models import ArtifactKind, ArtifactScope
from ai_code_change_agent.application.artifacts.redaction import find_secrets, redact_text
from ai_code_change_agent.application.artifacts.service import (
    REDACT_ON_SECRET,
    REFUSE_ON_SECRET,
    ArtifactService,
)
from ai_code_change_agent.execution.artifacts import LocalFilesystemArtifactStore

K = ArtifactKind
SCOPE = ArtifactScope(uuid.uuid4(), uuid.uuid4())
AWS = "AKIAABCDEFGHIJKLMNOP"
GH = "ghp_" + "a1B2c3D4e5" * 4
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\n-----END RSA PRIVATE KEY-----"


@pytest.fixture
def service(tmp_path: Path) -> ArtifactService:
    return ArtifactService(
        LocalFilesystemArtifactStore(tmp_path / "a"),
        max_artifact_bytes=2048,
        max_attempt_bytes=3000,
    )


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (f"key={AWS}", "aws_access_key"),
        (f"token {GH}", "github_token"),
        (PEM, "private_key"),
        ("sk-ant-" + "x1" * 20, "anthropic_key"),
        ("sk-" + "Ab1" * 10, "api_key"),
        ("xoxb-1234567890-abcdef", "slack_token"),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop", "jwt"),
        ("postgres://app:hunter22@db:5432/x", "url_credentials"),
        ('password = "correct-horse-battery"', "assigned_secret"),
        ("API_KEY: 9f8e7d6c5b4a", "assigned_secret"),
    ],
)
def test_known_secret_shapes_are_found(text: str, kind: str) -> None:
    assert [f.kind for f in find_secrets(text)] == [kind]


@pytest.mark.parametrize(
    "text",
    [
        "def greet(name): return f'Hello, {name}'",
        "password = '<your-password>'",
        "token: ${TOKEN}",
        "secret = changeme",
        "api_key = None",
        "the quick brown fox",
        "sha256 abc123",
        "# set the token to xxxxxxxx",
        "token = get_token()",
        "password = config.password",
        'api_key = os.environ["API_KEY"]',
        "secret = settings.secret_value",
    ],
)
def test_ordinary_code_and_placeholders_are_not_secrets(text: str) -> None:
    assert find_secrets(text) == []


def test_redaction_replaces_values_and_keeps_surrounding_text() -> None:
    cleaned, count = redact_text(f"a {AWS} b {GH} c")
    assert count == 2 and AWS not in cleaned and GH not in cleaned
    assert cleaned.startswith("a [REDACTED:aws_access_key] b ") and cleaned.endswith(" c")


def test_every_artifact_kind_has_exactly_one_policy() -> None:
    assert set(K) == REFUSE_ON_SECRET | REDACT_ON_SECRET
    assert not REFUSE_ON_SECRET & REDACT_ON_SECRET


@pytest.mark.parametrize("kind", sorted(REFUSE_ON_SECRET))
async def test_artifacts_that_are_hashed_and_applied_are_refused_never_altered(
    service: ArtifactService, kind: ArtifactKind
) -> None:
    with pytest.raises(ArtifactSecretError) as caught:
        await service.put(SCOPE, kind, f"+ key = '{AWS}'".encode())
    assert AWS not in repr(caught.value)
    assert await service.list(SCOPE) == []  # nothing was stored


@pytest.mark.parametrize("kind", sorted(REDACT_ON_SECRET))
async def test_reports_and_evidence_are_stored_redacted_with_the_stored_hash(
    service: ArtifactService, kind: ArtifactKind
) -> None:
    meta = await service.put(SCOPE, kind, f"leaked {GH} here".encode())
    stored = await service.get(SCOPE, meta.id)
    assert GH.encode() not in stored and b"[REDACTED:github_token]" in stored
    assert meta.redactions == 1 and meta.redaction_version == "1"
    import hashlib

    assert meta.sha256 == hashlib.sha256(stored).hexdigest()  # the hash of what was really stored


async def test_clean_content_is_stored_unchanged(service: ArtifactService) -> None:
    meta = await service.put(SCOPE, K.IMPLEMENTATION_PLAN, b'{"summary":"fine"}')
    assert await service.get(SCOPE, meta.id) == b'{"summary":"fine"}' and meta.redactions == 0


async def test_binary_content_cannot_be_scanned_so_it_is_refused(service: ArtifactService) -> None:
    with pytest.raises(ArtifactRedactionError):
        await service.put(SCOPE, K.VALIDATION_EVIDENCE, b"\xff\xfe\x00binary")
    assert await service.list(SCOPE) == []


async def test_scanner_failure_fails_closed(
    service: ArtifactService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_: str) -> list[object]:
        raise RuntimeError("scanner crashed")

    monkeypatch.setattr("ai_code_change_agent.application.artifacts.service.find_secrets", boom)
    with pytest.raises(ArtifactRedactionError):
        await service.put(SCOPE, K.IMPLEMENTATION_PLAN, b"{}")
    assert await service.list(SCOPE) == []


async def test_size_limits_per_artifact_and_per_attempt(service: ArtifactService) -> None:
    with pytest.raises(ArtifactTooLargeError):
        await service.put(SCOPE, K.IMPLEMENTATION_PLAN, b"x" * 2049)
    await service.put(SCOPE, K.IMPLEMENTATION_PLAN, b"x" * 2000)
    with pytest.raises(ArtifactTooLargeError, match="quota"):
        await service.put(SCOPE, K.IMPLEMENTATION_PLAN, b"x" * 1500)


async def test_service_delegates_reads_to_the_scoped_store(service: ArtifactService) -> None:
    meta = await service.put(SCOPE, K.IMPLEMENTATION_PLAN, b"hello")
    assert await service.get(SCOPE, meta.id) == b"hello"
    assert await service.meta(SCOPE, meta.id) == meta
    assert await service.exists(SCOPE, meta.id)
    assert await service.verify(SCOPE, meta.id) == meta
    assert await service.list(SCOPE) == [meta]
