"""Gate D.1: private key loading, App JWT and installation tokens (fake transport only)."""

import os
import time
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from ai_code_change_agent.integrations.github.authentication import (
    JWT_LIFETIME_SECONDS,
    GitHubAppAuth,
    GitHubAuthError,
)
from ai_code_change_agent.integrations.github.credentials import (
    GitHubAppConfigError,
    GitHubAppCredentials,
    load_credentials,
    load_private_key,
)
from ai_code_change_agent.integrations.github.publisher import API_BASE
from tests.support.fake_github import FakeGitHub, Fault, make_key

REPO = 424242


# ---- private key file -----------------------------------------------------------------------
def test_a_valid_key_file_loads(tmp_path: Path) -> None:
    key = make_key(tmp_path / "k.pem")
    loaded = load_private_key(str(tmp_path / "k.pem"))
    assert loaded.public_key().public_numbers() == key.public_key().public_numbers()


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_a_key_readable_by_others_is_refused(tmp_path: Path, mode: int) -> None:
    make_key(tmp_path / "k.pem", mode=mode)
    with pytest.raises(GitHubAppConfigError) as err:
        load_private_key(str(tmp_path / "k.pem"))
    assert err.value.code == "private_key_unavailable"


def test_missing_garbage_small_and_non_rsa_keys_are_refused(tmp_path: Path) -> None:
    (tmp_path / "garbage.pem").write_text("not a key")
    (tmp_path / "garbage.pem").chmod(0o600)
    make_key(tmp_path / "small.pem", bits=1024)
    ec_key = ec.generate_private_key(ec.SECP256R1())
    (tmp_path / "ec.pem").write_bytes(
        ec_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )  # fmt: skip
    (tmp_path / "ec.pem").chmod(0o600)
    for name in ("missing.pem", "garbage.pem", "small.pem", "ec.pem"):
        with pytest.raises(GitHubAppConfigError) as err:
            load_private_key(str(tmp_path / name))
        assert err.value.code == "private_key_unavailable"
        assert str(tmp_path) not in str(err.value)  # no path in the message


def test_symlinked_relative_and_directory_paths_are_refused(tmp_path: Path) -> None:
    make_key(tmp_path / "k.pem")
    os.symlink(tmp_path / "k.pem", tmp_path / "link.pem")
    with pytest.raises(GitHubAppConfigError):
        load_private_key(str(tmp_path / "link.pem"))
    with pytest.raises(GitHubAppConfigError) as err:
        load_private_key("relative/k.pem")
    assert err.value.code == "github_app_config_invalid"
    with pytest.raises(GitHubAppConfigError):
        load_private_key(str(tmp_path))


def test_incomplete_configuration_is_refused(tmp_path: Path) -> None:
    make_key(tmp_path / "k.pem")
    for args in ((None, 2, str(tmp_path / "k.pem")), (1, None, str(tmp_path / "k.pem")),
                 (1, 2, None), (0, 2, str(tmp_path / "k.pem"))):  # fmt: skip
        with pytest.raises(GitHubAppConfigError) as err:
            load_credentials(*args)
        assert err.value.code == "github_app_config_invalid"


def test_credentials_never_render_the_key(tmp_path: Path) -> None:
    creds = load_credentials(1, 2, str(make_key_path(tmp_path)))
    assert "PRIVATE" not in repr(creds) and "RSA" not in repr(creds) and "2" not in repr(creds)[-8:]


def make_key_path(tmp_path: Path) -> Path:
    make_key(tmp_path / "k.pem")
    return tmp_path / "k.pem"


# ---- JWT and installation token -------------------------------------------------------------
class Clock:
    def __init__(self) -> None:
        self.now = float(int(time.time()))  # JWT validation in the fake uses real time

    def __call__(self) -> float:
        return self.now


def rig(tmp_path: Path) -> tuple[GitHubAppAuth, FakeGitHub, Clock]:
    key = make_key(tmp_path / "k.pem")
    clock = Clock()
    hub = FakeGitHub(tmp_path / "unused.git", public_key=key.public_key(), clock=clock)
    auth = GitHubAppAuth(GitHubAppCredentials(hub.app_id, hub.installation_id, key), clock=clock)
    return auth, hub, clock


def client(hub: FakeGitHub) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=API_BASE, transport=hub.transport(), follow_redirects=False)


def test_the_app_jwt_is_rs256_short_lived_and_signed_by_the_key(tmp_path: Path) -> None:
    auth, hub, clock = rig(tmp_path)
    token = auth.app_jwt()
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = jwt.decode(token, hub.public_key, algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == str(hub.app_id)
    assert (
        claims["exp"] - claims["iat"] <= 600 and claims["exp"] == clock.now + JWT_LIFETIME_SECONDS
    )


async def test_the_token_is_scoped_cached_and_refreshed_before_expiry(tmp_path: Path) -> None:
    auth, hub, clock = rig(tmp_path)
    async with client(hub) as c:
        first = await auth.token(c, REPO)
        assert await auth.token(c, REPO) == first and hub.issued == 1  # cached
        clock.now += 3600 - 299  # inside the five minute refresh margin
        second = await auth.token(c, REPO)
    assert second != first and hub.issued == 2


async def test_invalidate_forces_one_fresh_exchange(tmp_path: Path) -> None:
    auth, hub, _ = rig(tmp_path)
    async with client(hub) as c:
        await auth.token(c, REPO)
        auth.invalidate(REPO)
        await auth.token(c, REPO)
    assert hub.issued == 2


@pytest.mark.parametrize(
    ("tweak", "code"),
    [
        (lambda h: setattr(h, "installation_present", False), "installation_missing"),
        (lambda h: setattr(h, "app_id", 999), "app_jwt_invalid"),
        (lambda h: setattr(h, "accessible", set()), "installation_access_denied"),
        (lambda h: h.granted.update(contents="read"), "installation_access_denied"),
        (lambda h: h.faults.append(Fault("POST", "access_tokens", status=429)),
         "provider_rate_limited"),
        (lambda h: h.faults.append(Fault("POST", "access_tokens", timeout=True)),
         "provider_timeout"),
        (lambda h: h.faults.append(Fault("POST", "access_tokens", status=503)),
         "provider_unavailable"),
        (lambda h: h.faults.append(Fault("POST", "access_tokens", status=403)),
         "installation_auth_failed"),
    ],
)  # fmt: skip
async def test_exchange_failures_have_fixed_codes(tmp_path: Path, tweak: object, code: str) -> None:
    auth, hub, _ = rig(tmp_path)
    tweak(hub)  # type: ignore[operator]
    async with client(hub) as c:
        with pytest.raises(GitHubAuthError) as err:
            await auth.token(c, REPO)
    assert err.value.code == code


@pytest.mark.parametrize(
    "body",
    [
        {"token": "t", "expires_at": "2027-01-01T00:00:00Z",
         "permissions": {"metadata": "read", "contents": "read", "pull_requests": "write"},
         "repositories": [{"id": REPO}]},
        {"token": "t", "expires_at": "2027-01-01T00:00:00Z",
         "permissions": {"metadata": "read", "contents": "write", "pull_requests": "write"},
         "repositories": [{"id": REPO}, {"id": 7}]},
        {"token": "", "expires_at": "2027-01-01T00:00:00Z", "permissions": {}},
        {"expires_at": "soon"},
    ],
)  # fmt: skip
async def test_a_response_granting_less_or_more_than_asked_is_refused(
    tmp_path: Path, body: dict[str, object]
) -> None:
    auth, hub, _ = rig(tmp_path)
    hub.faults.append(Fault("POST", "access_tokens", status=201, body=body))
    async with client(hub) as c:
        with pytest.raises(GitHubAuthError) as err:
            await auth.token(c, REPO)
    assert err.value.code in {"permission_missing", "repository_inaccessible",
                              "installation_auth_failed"}  # fmt: skip
