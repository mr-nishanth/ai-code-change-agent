"""Operator CLI wiring on DynamoDB, SQS and S3 (unit level; Floci scenarios live in
`tests/integration/floci/test_cli_dynamo.py`)."""

import argparse
import asyncio
import contextlib
import io
import json
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from ai_code_change_agent.application.multirepo.coordinator import MultiRepoBlockedError
from ai_code_change_agent.cli import main as cli_main
from ai_code_change_agent.cli import operations
from ai_code_change_agent.cli.stores import CliStores
from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.core.logging import REDACTED, get_logger
from ai_code_change_agent.domain.changes.models import ApprovalDecision, ApprovalGate
from ai_code_change_agent.domain.webhooks.models import PersistenceError
from ai_code_change_agent.execution.s3_artifacts import S3ArtifactStore
from ai_code_change_agent.orchestration.intake import RESUME
from ai_code_change_agent.persistence.dynamodb.job_queue import DynamoSqsJobQueue
from ai_code_change_agent.persistence.dynamodb.registry_store import DynamoRegistryUnitOfWork
from ai_code_change_agent.persistence.dynamodb.usage_store import DynamoUsageStore

CLI_SRC = Path(operations.__file__).parent


def local(**env: object) -> Settings:
    return Settings.model_validate(
        {"app_env": "test", "ACCA_AWS_ENDPOINT_URL": "http://127.0.0.1:4566", **env}
    )


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli_main.main(list(argv), out=out, err_out=err)
    return code, out.getvalue(), err.getvalue()


class FakeQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, dict[str, Any], str, int]] = []

    async def enqueue(
        self, kind: str, payload: dict[str, Any], *, dedupe_key: str, max_attempts: int
    ) -> bool:
        self.jobs.append((kind, payload, dedupe_key, max_attempts))
        return True


class FakeChanges:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.decided: list[tuple[uuid.UUID, ApprovalGate, str, ApprovalDecision, str]] = []
        self.reasons: list[str | None] = []

    async def decide(
        self,
        aid: uuid.UUID,
        gate: ApprovalGate,
        sha: str,
        decision: ApprovalDecision,
        actor: str,
        *,
        reason: str | None = None,
    ) -> tuple[Any, Any]:
        if self.error is not None:
            raise self.error
        self.decided.append((aid, gate, sha, decision, actor))
        self.reasons.append(reason)
        status = SimpleNamespace(value="PLAN_APPROVED")
        return SimpleNamespace(id=uuid.UUID(int=7)), SimpleNamespace(id=aid, status=status)


class FakeStores:
    def __init__(self, changes: FakeChanges, coordinator: Any = None) -> None:
        self.settings = local(job_max_attempts=4)
        self.changes = changes
        self.queue = FakeQueue()
        self._coordinator = coordinator

    def job_queue(self) -> FakeQueue:
        return self.queue

    def coordinator(self) -> Any:
        return self._coordinator


def use(monkeypatch: pytest.MonkeyPatch, stores: FakeStores) -> None:
    monkeypatch.setattr(operations, "CliStores", lambda _settings: stores)


@pytest.mark.parametrize(("command", "decision"), [("approve", "APPROVED"), ("reject", "REJECTED")])
def test_a_decision_wakes_the_workflow_through_the_dynamodb_job_queue(
    monkeypatch: pytest.MonkeyPatch, command: str, decision: str
) -> None:
    stores = FakeStores(FakeChanges())
    use(monkeypatch, stores)
    aid = uuid.uuid4()
    code, out, err = run("changes", command, str(aid), "--gate", "FINAL", "--sha256", "f" * 64)
    assert code == 0 and err == ""
    assert out == f"{decision} FINAL for attempt {aid}; status PLAN_APPROVED\n"
    (_, gate, sha, made, actor) = stores.changes.decided[0]
    assert (gate, sha, made.value) == (ApprovalGate.FINAL, "f" * 64, decision)
    assert actor.startswith("cli:")
    assert stores.queue.jobs == [
        (RESUME, {"attempt_id": str(aid)}, f"{RESUME}:{aid}:{uuid.UUID(int=7)}", 4)
    ]


def test_a_rejection_records_the_operator_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    stores = FakeStores(FakeChanges())
    use(monkeypatch, stores)
    aid = str(uuid.uuid4())
    sha = "f" * 64
    assert run("changes", "reject", aid, "--gate", "PLAN", "--sha256", sha)[0] == 0
    code, _, err = run(
        "changes", "reject", aid, "--gate", "PLAN", "--sha256", sha, "--reason", "  wrong module  "
    )
    assert (code, err) == (0, "")
    assert stores.changes.reasons == [None, "wrong module"]  # optional; surrounding space dropped


@pytest.mark.parametrize("reason", ["", "   ", "two\nlines", "tab\there", "x" * 501])
def test_a_reason_that_is_empty_multi_line_or_too_long_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], reason: str
) -> None:
    stores = FakeStores(FakeChanges())
    use(monkeypatch, stores)
    code, out, _ = run(
        "changes", "reject", str(uuid.uuid4()), "--gate", "PLAN", "--sha256", "f" * 64,
        "--reason", reason,
    )  # fmt: skip
    assert code == 2 and out == "" and "argument --reason" in capsys.readouterr().err  # argparse
    assert stores.changes.decided == [] and stores.queue.jobs == []


def test_approve_takes_no_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    stores = FakeStores(FakeChanges())
    use(monkeypatch, stores)
    code, _, _ = run(
        "changes", "approve", str(uuid.uuid4()), "--gate", "PLAN", "--sha256", "f" * 64,
        "--reason", "fine",
    )  # fmt: skip
    assert code == 2 and stores.changes.decided == []


def test_a_refused_decision_enqueues_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_code_change_agent.domain.changes.errors import StaleApprovalError

    stores = FakeStores(FakeChanges(StaleApprovalError("artifact_changed")))
    use(monkeypatch, stores)
    code, _, err = run("changes", "approve", str(uuid.uuid4()), "--gate", "PLAN", "--sha256", "a")
    assert code == 1 and "artifact_changed" in err
    assert stores.queue.jobs == []


def test_storage_failures_print_only_the_error_code(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, FakeStores(FakeChanges(PersistenceError("ResourceNotFoundException"))))
    code, out, err = run("changes", "approve", str(uuid.uuid4()), "--gate", "PLAN", "--sha256", "a")
    assert (code, out, err) == (1, "", "error: storage: ResourceNotFoundException\n")


def test_retry_child_goes_through_the_coordinator(monkeypatch: pytest.MonkeyPatch) -> None:
    child_id = uuid.uuid4()

    class Coordinator:
        async def retry(self, cid: uuid.UUID) -> Any:
            if cid != child_id:
                raise MultiRepoBlockedError("child_not_found", str(cid))
            return SimpleNamespace(
                id=cid, repository_key="shop-api", status=SimpleNamespace(value="WAITING")
            )

    use(monkeypatch, FakeStores(FakeChanges(), Coordinator()))
    assert run("changes", "retry-child", str(child_id)) == (
        0,
        f"child {child_id} (shop-api) is WAITING\n",
        "",
    )
    code, _, err = run("changes", "retry-child", str(uuid.uuid4()))
    assert code == 1 and err == "error: child_not_found\n"


def test_registry_storage_failures_exit_1_with_the_code_only() -> None:
    class Broken:
        async def list_repositories(self) -> list[Any]:
            raise PersistenceError("ThrottlingException")

    out, err = io.StringIO(), io.StringIO()
    code = cli_main.main(
        ["repositories", "list"],
        service_factory=lambda: (Broken(), None),  # type: ignore[return-value,arg-type]
        out=out,
        err_out=err,
    )
    assert (code, err.getvalue()) == (1, "error: storage: ThrottlingException\n")


def test_stores_are_dynamodb_sqs_and_s3_adapters_from_settings() -> None:
    settings = local(
        ACCA_DYNAMODB_TABLE_NAME="s2116-cli-unit",
        ACCA_SQS_JOB_QUEUE_NAME="s2116-cli-jobs",
        ACCA_S3_BUCKET_NAME="s2116-cli-bucket",
    )
    stores = CliStores(settings)  # builds clients only: no AWS call
    assert stores.table.name == "s2116-cli-unit"
    assert stores.aws.endpoint_url == "http://127.0.0.1:4566"
    assert isinstance(stores.usage, DynamoUsageStore)
    assert isinstance(stores.registry_uow(), DynamoRegistryUnitOfWork)
    queue = stores.job_queue()
    assert isinstance(queue, DynamoSqsJobQueue)
    assert queue._queue_url == stores.aws.queue_url  # resolved by name on first send
    artifact_store = stores.artifacts()._store
    assert isinstance(artifact_store, S3ArtifactStore)
    assert artifact_store._bucket == "s2116-cli-bucket"
    coordinator = stores.coordinator()
    assert isinstance(coordinator._queue, DynamoSqsJobQueue)
    assert coordinator._store.__class__.__name__ == "DynamoMultiRepoStore"


def test_default_registry_services_use_dynamodb(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_code_change_agent.core.config import get_settings

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ACCA_DYNAMODB_TABLE_NAME", "s2116-cli-default")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    try:
        svc, resolver = cli_main._default_services()
    finally:
        get_settings.cache_clear()
    uow = svc._uow()
    assert isinstance(uow, DynamoRegistryUnitOfWork)
    assert uow.table.name == "s2116-cli-default"
    assert resolver is not None


def test_the_cli_has_no_sql_database_code() -> None:
    for path in CLI_SRC.glob("*.py"):
        text = path.read_text()
        for word in ("Postgres", "SqlAlchemy", "sqlalchemy", "create_engine", "DATABASE_URL"):
            assert word not in text, (path.name, word)


def test_the_worker_writes_its_output_line_by_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker redirected to a log file must not hold lines in a block buffer until exit
    (observed: empty log files unless PYTHONUNBUFFERED=1)."""
    out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    assert not out.line_buffering
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    ran: list[bool] = []

    async def worker(once: bool) -> None:
        ran.append(once)

    monkeypatch.setattr(operations, "_worker", worker)
    assert operations.run(argparse.Namespace(area="worker", once=True), out) == 0
    assert ran == [True] and out.line_buffering and err.line_buffering


def test_the_worker_configures_redacting_logs_before_it_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    structlog.reset_defaults()  # what a fresh process has: no redaction processor
    settings = local().model_copy(update={"log_level": "INFO", "log_json": True})
    monkeypatch.setattr(operations, "get_settings", lambda: settings)

    class Worker:
        async def run_once(self) -> None:
            get_logger("worker").info("job_started", api_token="abc123secret", job="j1")

    class Costs:
        async def print_session_summary(self) -> None:
            return None

    @contextlib.asynccontextmanager
    async def worker_app(_settings: Settings) -> AsyncIterator[Any]:
        yield SimpleNamespace(worker=Worker(), costs=Costs())

    monkeypatch.setattr(operations, "worker_app", worker_app)
    asyncio.run(operations._worker(once=True))
    out = capsys.readouterr().out
    line = json.loads(next(ln for ln in out.splitlines() if "job_started" in ln))
    assert line["api_token"] == REDACTED and line["job"] == "j1"
    assert "abc123secret" not in out
