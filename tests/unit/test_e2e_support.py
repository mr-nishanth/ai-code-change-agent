"""The e2e suite's skip rules (tests/e2e/conftest.py): never a failure, always a reason."""

import shutil
import subprocess

import pytest

from tests.e2e import conftest as e2e


def _docker(live: str = "", image: int = 0, daemon: int = 0) -> object:
    def run(*args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        if args[0] == "version":
            return subprocess.CompletedProcess(args, daemon, "27.0.0\n", "")
        if args[0] == "image":
            return subprocess.CompletedProcess(args, image, "", "")
        return subprocess.CompletedProcess(args, 0, live, "")

    return run


@pytest.fixture
def ready(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setattr(e2e, "floci_reachable", lambda: None)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(e2e, "docker", _docker())
    return monkeypatch


def test_everything_ready_means_no_skip(ready: pytest.MonkeyPatch) -> None:
    assert e2e.skip_reason() is None


def test_a_live_sandbox_is_a_skip_with_a_reason(ready: pytest.MonkeyPatch) -> None:
    ready.setattr(e2e, "docker", _docker(live="abc123\ndef456\n"))
    assert e2e.skip_reason() == (
        "2 live agent sandbox(es) running; e2e tests share the Docker daemon"
    )


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"floci": "Floci not reachable at x"}, "Floci not reachable at x"),
        ({"which": None}, "docker CLI not found"),
        ({"daemon": 1}, "docker daemon not reachable"),
        ({"image": 1}, "is not built"),
    ],
)
def test_missing_prerequisites_are_skips_with_a_reason(
    ready: pytest.MonkeyPatch, change: dict[str, object], reason: str
) -> None:
    if "floci" in change:
        ready.setattr(e2e, "floci_reachable", lambda: change["floci"])
    if "which" in change:
        ready.setattr(shutil, "which", lambda _name: None)
    ready.setattr(
        e2e,
        "docker",
        _docker(image=int(str(change.get("image", 0))), daemon=int(str(change.get("daemon", 0)))),
    )
    found = e2e.skip_reason()
    assert found is not None and reason in found
