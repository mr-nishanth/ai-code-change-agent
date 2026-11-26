"""Harness for the GitHub publisher: a real Git repository served by the fake GitHub, and a valid
DeliveryRequest built from Git's own view of the base commit."""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ai_code_change_agent.application.git_providers.delivery import (
    BaselineEntry,
    DeliveryReceipt,
    DeliveryRequest,
    DeliveryTarget,
    PullRequestSpec,
)
from ai_code_change_agent.application.workspaces.models import ChangeOp, FileChange
from ai_code_change_agent.application.workspaces.paths import tree_hash
from ai_code_change_agent.integrations.github.publisher import GitHubAppPublisher
from tests.support.fake_github import FakeGitHub
from tests.support.github_repos import blob_bytes, reader_rig

CHANGES = (
    FileChange("src/app.py", ChangeOp.MODIFY, b"def greet():\n    return 'Goodbye'\n"),
    FileChange("docs/new.md", ChangeOp.CREATE, b"# New\n"),
    FileChange("tests/test_app.py", ChangeOp.DELETE),
)


@dataclass
class PublishRig:
    hub: FakeGitHub
    publisher: GitHubAppPublisher
    request: DeliveryRequest
    base: str

    async def publish(self) -> DeliveryReceipt:
        return await self.publisher.publish(self.request)


def publish_rig(tmp_path: Path, changes: tuple[FileChange, ...] = CHANGES) -> PublishRig:
    rig = reader_rig(tmp_path)
    files = blob_bytes(rig.bare, rig.sha)
    platform = {p: (x, hashlib.sha256(c).hexdigest()) for p, (c, x) in files.items()}
    baseline_hash = tree_hash((p, x, h) for p, (x, h) in platform.items())
    for c in changes:
        if c.op is ChangeOp.DELETE:
            platform.pop(c.path)
        else:
            assert c.content is not None
            platform[c.path] = (
                platform.get(c.path, (False, ""))[0],
                hashlib.sha256(c.content).hexdigest(),
            )
    request = DeliveryRequest(
        target=DeliveryTarget(uuid.uuid4(), "github", "424242", "acme", "shop-service", "main"),
        base_commit_sha=rig.sha,
        baseline_tree_hash=baseline_hash,
        result_tree_hash=tree_hash((p, x, h) for p, (x, h) in platform.items()),
        branch="aica/shop-1-a1-0123456789ab",
        changes=changes,
        commit_message="AI Code Change: SHOP-1 (attempt 1)\n",
        commit_time=datetime(2026, 1, 1, tzinfo=UTC),
        manifest_sha256="a" * 64,
        baseline=tuple(
            BaselineEntry(p, x, hashlib.sha256(c).hexdigest()) for p, (c, x) in files.items()
        ),
        pull_request=PullRequestSpec("[SHOP-1] AI Code Change Agent change", "body"),
    )
    publisher = GitHubAppPublisher(auth=rig.auth, transport=rig.hub.transport())
    return PublishRig(rig.hub, publisher, request, rig.sha)
