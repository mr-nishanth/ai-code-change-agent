"""Gate D.1: publisher preflight (no I/O) and remaining remote edge cases."""

import base64
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ai_code_change_agent.application.delivery.errors import DeliveryError
from ai_code_change_agent.application.git_providers.delivery import (
    BaselineEntry,
    DeliveryRequest,
    DeliveryTarget,
    PullRequestSpec,
)
from ai_code_change_agent.application.workspaces.models import ChangeOp, FileChange
from ai_code_change_agent.integrations.github.authentication import GitHubAppAuth
from ai_code_change_agent.integrations.github.credentials import (
    GitHubAppCredentials,
)
from ai_code_change_agent.integrations.github.publisher import (
    GitHubAppPublisher,
)
from tests.support.fake_github import Fault, make_key
from tests.support.publishing import publish_rig


class NoNetwork:
    def __init__(self) -> None:
        self.calls = 0

    def handle(self, request: Any) -> Any:  # pragma: no cover - must never run
        self.calls += 1
        raise AssertionError("no request expected")


def base_request(**over: Any) -> DeliveryRequest:
    target = DeliveryTarget(
        repository_id=uuid.uuid4(), git_provider="github", provider_repository_id="424242",
        owner="acme", name="shop-service", default_branch="main",
    )  # fmt: skip
    req = DeliveryRequest(
        target=target, base_commit_sha="a" * 40, baseline_tree_hash="1" * 64,
        result_tree_hash="2" * 64, branch="aica/shop-1-a1-0123456789ab",
        changes=(FileChange("src/app.py", ChangeOp.MODIFY, b"x"),), commit_message="m",
        commit_time=datetime(2026, 1, 1, tzinfo=UTC), manifest_sha256="3" * 64,
        baseline=(BaselineEntry("src/app.py", False, "4" * 64),),
        pull_request=PullRequestSpec("t", "b"),
    )  # fmt: skip
    return replace(req, **over)


def publisher(tmp_path: Path) -> GitHubAppPublisher:
    import httpx

    key = make_key(tmp_path / "k.pem")
    guard = NoNetwork()
    return GitHubAppPublisher(
        auth=GitHubAppAuth(GitHubAppCredentials(1, 2, key)),
        transport=httpx.MockTransport(guard.handle),
    )  # fmt: skip


def target(**kw: Any) -> DeliveryTarget:
    return replace(base_request().target, **kw)


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"target": target(git_provider="gitlab")}, "target_identity_invalid"),
        ({"target": target(owner="a/b")}, "target_identity_invalid"),
        ({"target": target(name="x?y")}, "target_identity_invalid"),
        ({"target": target(provider_repository_id="abc")}, "target_identity_invalid"),
        ({"target": target(default_branch="main;rm")}, "target_identity_invalid"),
        ({"branch": "main"}, "delivery_request_invalid"),
        ({"branch": "refs/heads/main"}, "delivery_request_invalid"),
        ({"base_commit_sha": "a" * 64}, "delivery_request_invalid"),
        ({"pull_request": None}, "delivery_request_invalid"),
        ({"baseline": ()}, "delivery_request_invalid"),
        ({"changes": ()}, "delivery_request_invalid"),
        ({"result_tree_hash": "z"}, "delivery_request_invalid"),
        ({"changes": (FileChange("../etc/passwd", ChangeOp.CREATE, b"x"),)}, "unsafe_path"),
        ({"changes": (FileChange(".github/workflows/x.yml", ChangeOp.CREATE, b"x"),)},
         "protected_path"),
        ({"changes": (FileChange("a.py", ChangeOp.DELETE, b"x"),)}, "delivery_request_invalid"),
        ({"changes": (FileChange("a.py", ChangeOp.CREATE, None),)}, "delivery_request_invalid"),
    ],
)  # fmt: skip
async def test_preflight_refuses_before_any_request(
    tmp_path: Path, over: dict[str, Any], code: str
) -> None:
    with pytest.raises(DeliveryError) as err:
        await publisher(tmp_path).publish(base_request(**over))
    assert err.value.code == code


# ---- end to end against the git-backed fake (no network) ----------------------------------
WRITE_SCOPE = {"metadata": "read", "contents": "write", "pull_requests": "write"}


async def test_a_verified_draft_pull_request_is_created_on_the_pinned_base(tmp_path: Path) -> None:
    x = publish_rig(tmp_path)
    receipt = await x.publish()
    assert receipt.outcome.value == "PULL_REQUEST_CREATED" and receipt.created
    (pr,) = x.hub.pulls.values()
    assert pr["draft"] is True and pr["base"]["ref"] == "main"
    assert pr["head"]["sha"] == receipt.commit_sha == receipt.pr_head_sha
    assert x.hub.git("rev-parse", f"{receipt.commit_sha}^") == x.base
    assert (
        x.hub.git("show", f"{receipt.commit_sha}:src/app.py")
        == "def greet():\n    return 'Goodbye'"
    )
    assert x.hub.ref("main") == x.base  # the default branch is never written
    assert all(p == WRITE_SCOPE for p, _ in x.hub.token_meta.values())
    for method, path in x.hub.writes():
        assert method == "POST" and "merge" not in path


async def test_a_repeated_delivery_reuses_the_branch_and_pull_request(tmp_path: Path) -> None:
    x = publish_rig(tmp_path)
    first = await x.publish()
    second = await x.publish()
    assert second.outcome.value == "PULL_REQUEST_CREATED" and not second.created
    assert second.pr_number == first.pr_number and len(x.hub.pulls) == 1


async def test_a_repository_removed_from_the_installation_is_refused(tmp_path: Path) -> None:
    x = publish_rig(tmp_path)
    x.hub.accessible = set()
    receipt = await x.publish()
    assert receipt.failure_code == "installation_access_denied" and x.hub.pulls == {}


async def test_a_changed_file_whose_remote_bytes_differ_is_refused(tmp_path: Path) -> None:
    x = publish_rig(tmp_path)
    x.hub.faults.append(
        Fault(
            "GET",
            r"/git/blobs/",
            status=200,
            body={
                "encoding": "base64",
                "content": base64.b64encode(b"tampered").decode(),
            },
        )
    )
    receipt = await x.publish()
    assert receipt.failure_code == "baseline_mismatch" and x.hub.pulls == {}


async def test_a_server_error_on_a_write_is_an_unknown_outcome(tmp_path: Path) -> None:
    x = publish_rig(tmp_path)
    x.hub.faults.append(Fault("POST", r"/git/trees$", status=502))
    receipt = await x.publish()
    assert receipt.outcome.value == "DELIVERY_OUTCOME_UNKNOWN"


async def test_a_forbidden_write_is_a_permission_failure(tmp_path: Path) -> None:
    x = publish_rig(tmp_path)
    x.hub.faults.append(Fault("POST", r"/git/blobs$", status=403))
    receipt = await x.publish()
    assert receipt.failure_code == "permission_missing"


async def test_a_ref_created_concurrently_with_the_same_commit_is_reused(tmp_path: Path) -> None:
    x = publish_rig(tmp_path)
    x.hub.faults.append(Fault("GET", r"/git/ref/heads/", status=404))  # looked absent at first
    x.hub.faults.append(Fault("POST", r"/git/refs$", when="after", status=422))
    receipt = await x.publish()
    assert receipt.outcome.value == "PULL_REQUEST_CREATED"


async def test_a_pull_request_that_cannot_be_re_read_is_not_claimed(tmp_path: Path) -> None:
    x = publish_rig(tmp_path)
    x.hub.faults.append(Fault("GET", r"/pulls/\d+$", status=404))
    receipt = await x.publish()
    assert receipt.outcome.value == "DELIVERY_OUTCOME_UNKNOWN"
    assert (await x.publish()).outcome.value == "PULL_REQUEST_CREATED"  # reconciled later


async def test_a_pull_request_url_on_another_host_is_refused(tmp_path: Path) -> None:
    x = publish_rig(tmp_path)
    original = x.hub._get_pull

    def evil(*args: Any) -> Any:
        response = original(*args)
        body = response.json() | {"html_url": "https://evil.example/acme/shop-service/pull/1"}
        import httpx

        return httpx.Response(200, json=body)

    x.hub._get_pull = evil  # type: ignore[method-assign,assignment]
    receipt = await x.publish()
    assert receipt.outcome.value != "PULL_REQUEST_CREATED" and receipt.failure_code
