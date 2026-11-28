"""Operator commands for the workflow: run the worker, inspect an attempt, record approvals.

Approvals are bound to the exact artifact hash shown by `changes show`; the actor is the OS user
and is recorded as unverified until operator SSO exists. A decision only wakes the workflow (a
resume job); the workflow re-reads and re-checks the approval itself."""

import argparse
import asyncio
import contextlib
import getpass
import json
import re
import signal
import sys
import uuid
from typing import TextIO

from ai_code_change_agent.application.artifacts.redaction import find_secrets
from ai_code_change_agent.application.jira.reports import ReportRecord, ReportState
from ai_code_change_agent.cli.stores import CliStores
from ai_code_change_agent.composition import build_cost_reporter, worker_app
from ai_code_change_agent.core.config import get_settings
from ai_code_change_agent.core.logging import configure_logging, get_logger
from ai_code_change_agent.domain.changes.models import (
    REASON,
    REASON_MAX,
    ApprovalDecision,
    ApprovalGate,
)
from ai_code_change_agent.orchestration.intake import RESUME
from ai_code_change_agent.persistence.dynamodb.job_queue import job_id_of_message as dlq_job_id
from ai_code_change_agent.persistence.dynamodb.keys import job as job_key

_log = get_logger("worker")


def add_parsers(top: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    worker = top.add_parser("worker", help="run the job worker (trusted host process)")
    worker.add_argument("--once", action="store_true", help="process at most one job and exit")
    reports = top.add_parser("reports", help="inspect durable Jira reports (pending, failed)")
    rsub = reports.add_subparsers(dest="command", required=True)
    rlist = rsub.add_parser("list", help="the newest reports and their delivery state")
    rlist.add_argument("--state", choices=[s.value for s in ReportState])
    rlist.add_argument("--limit", type=int, default=20)
    rshow = rsub.add_parser("show", help="one report (never its comment text)")
    rshow.add_argument("report_id")
    jobs = top.add_parser("jobs", help="inspect the job queue")
    jsub = jobs.add_subparsers(dest="command", required=True)
    dlq = jsub.add_parser("dlq", help="peek at dead-lettered messages and their jobs")
    dlq.add_argument("--max", type=int, default=10, choices=range(1, 11), metavar="1..10")
    costs = top.add_parser("costs", help="print LLM API cost reports from the usage ledger")
    costs.add_argument("--issue", action="append", default=[], help="Jira issue key (repeatable)")
    costs.add_argument("--attempt", type=uuid.UUID, action="append", default=[])
    costs.add_argument("--session", type=uuid.UUID, help="a worker session id")
    changes = top.add_parser("changes", help="inspect attempts and record approvals")
    sub = changes.add_subparsers(dest="command", required=True)
    retry = sub.add_parser(
        "retry-child",
        help="re-run one failed child of a multi-repository Story (and those it blocked)",
    )
    retry.add_argument("child", type=uuid.UUID)
    show = sub.add_parser("show", help="show an attempt and its approvals")
    show.add_argument("attempt", type=uuid.UUID)
    for name in ("approve", "reject"):
        p = sub.add_parser(name, help=f"{name} the plan or the final artifact by its hash")
        p.add_argument("attempt", type=uuid.UUID)
        p.add_argument("--gate", choices=("PLAN", "FINAL"), required=True)
        p.add_argument("--sha256", required=True, help="the exact artifact hash being decided")
        if name == "reject":
            p.add_argument(
                "--reason",
                type=_reason,
                help=f"why (one line, at most {REASON_MAX} characters; shown in the Jira comment)",
            )


def _reason(text: str) -> str:
    reason = text.strip()
    if not REASON.fullmatch(reason):
        raise argparse.ArgumentTypeError(
            f"one line of 1-{REASON_MAX} printable characters is required"
        )
    if find_secrets(reason):  # never stored: the reason is shown in Jira and kept in DynamoDB
        raise argparse.ArgumentTypeError("the reason looks like a secret; it was not recorded")
    return reason


def _actor() -> str:
    name = re.sub(r"[^A-Za-z0-9_.@-]", "_", getpass.getuser())[:64] or "unknown"
    return f"cli:{name}"


async def _worker(once: bool) -> None:
    settings = get_settings()
    # the API configures this in main.py; the worker is its own process and must do it too
    configure_logging(settings.log_level, json_output=settings.log_json)  # redacts secret keys
    async with worker_app(settings) as app:
        try:
            if once:
                await app.worker.run_once()
                return
            stop, force = asyncio.Event(), asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, _on_signal, sig, stop, force)
            await app.worker.run(stop, force)
        finally:
            await app.costs.print_session_summary()  # recomputed from the persisted ledger


def _on_signal(sig: signal.Signals, stop: asyncio.Event, force: asyncio.Event) -> None:
    """SIGINT and SIGTERM alike: the first stops claiming and gives a running job the worker's
    shutdown grace; a second one cancels it at once (its lease is released either way)."""
    if stop.is_set():
        force.set()
        _log.warning("worker_shutdown_forced", signal=sig.name)
    else:
        stop.set()
        _log.warning("worker_shutdown_requested", signal=sig.name)


async def _costs(args: argparse.Namespace, out: TextIO) -> None:
    """Task reports per attempt (and a session summary with --session), from the usage ledger."""
    stores = CliStores(get_settings())
    reporter = build_cost_reporter(
        stores.usage, stores.settings, stores.changes, args.session or uuid.uuid4(), out.write
    )
    attempts = list(args.attempt)
    if args.issue:
        rows = await stores.usage.rows(issue_keys=args.issue)
        attempts += sorted(
            {r.record.attempt_id for r in rows if r.record.attempt_id},
            key=lambda a: min(r.record.created_at for r in rows if r.record.attempt_id == a),
        )
    for attempt_id in dict.fromkeys(attempts):
        await reporter.attempt_report(attempt_id, crashed=True)
    if args.session:
        await reporter.print_session_summary()


async def _changes(args: argparse.Namespace, out: TextIO) -> None:
    stores = CliStores(get_settings())
    service = stores.changes
    if args.command == "retry-child":
        child = await stores.coordinator().retry(args.child)
        out.write(f"child {child.id} ({child.repository_key}) is {child.status.value}\n")
        return
    if args.command == "show":
        attempt = await service.get_attempt(args.attempt)
        approvals = await service.approvals(args.attempt)
        data = {
            "attempt_id": str(attempt.id), "status": attempt.status.value,
            "base_commit_sha": attempt.base_commit_sha, "plan_sha256": attempt.plan_sha256,
            "manifest_sha256": attempt.manifest_sha256, "repairs": attempt.repair_count,
            "failure_code": attempt.failure_code,
            "approvals": [
                {"gate": a.gate.value, "decision": a.decision.value,
                 "sha256": a.artifact_sha256,
                 "actor": a.actor, "actor_verified": a.actor_verified, "reason": a.reason}
                for a in approvals
            ],
        }  # fmt: skip
        out.write(json.dumps(data, indent=2) + "\n")
        return
    decision = ApprovalDecision.APPROVED if args.command == "approve" else ApprovalDecision.REJECTED
    approval, attempt = await service.decide(
        args.attempt, ApprovalGate(args.gate), args.sha256, decision, _actor(),
        reason=getattr(args, "reason", None),
    )  # fmt: skip
    # the workflow re-reads and re-checks the approval itself; this only wakes it
    await stores.job_queue().enqueue(
        RESUME, {"attempt_id": str(attempt.id)},
        dedupe_key=f"{RESUME}:{attempt.id}:{approval.id}",
        max_attempts=stores.settings.job_max_attempts,
    )  # fmt: skip
    out.write(
        f"{decision.value} {args.gate} for attempt {attempt.id}; status {attempt.status.value}\n"
    )


async def _reports(args: argparse.Namespace, out: TextIO) -> None:
    """Delivery state of app-posted Jira comments (DurableJiraReporter); never the text."""
    store = CliStores(get_settings()).reports()
    if args.command == "show":
        record = await store.get(args.report_id)
        if record is None:
            out.write(f"report {args.report_id} not found\n")
            return
        out.write(json.dumps(_report_row(record), indent=2) + "\n")
        return
    for record in await store.recent(max(1, min(args.limit, 200))):
        if args.state and record.state.value != args.state:
            continue
        row = _report_row(record)
        out.write(" ".join(f"{k}={v}" for k, v in row.items() if v is not None) + "\n")


def _report_row(record: ReportRecord) -> dict[str, object]:
    def when(moment: object) -> str | None:
        return moment.isoformat(timespec="seconds") if hasattr(moment, "isoformat") else None

    return {
        "report_id": record.report_id, "issue": record.issue_key, "state": record.state.value,
        "attempts": record.attempts, "next_retry_at": when(record.next_retry_at),
        "last_error": record.last_error, "comment_id": record.comment_id,
        "sha256": record.content_sha256[:12], "updated_at": when(record.updated_at),
    }  # fmt: skip


async def _jobs(args: argparse.Namespace, out: TextIO) -> None:
    """`jobs dlq`: what reached the dead-letter queue (job id, and the job's stored state). A peek:
    the messages stay in the DLQ, visible. A job that is not terminal is recovered by the worker's
    sweep (it re-sends a message for it); a terminal one needs nothing (a redrive is a no-op)."""
    stores = CliStores(get_settings())
    url = await asyncio.to_thread(stores.dlq_url)
    sqs = stores.aws.sqs
    attrs = (await asyncio.to_thread(
        sqs.get_queue_attributes, QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"]
    ))["Attributes"]  # fmt: skip
    out.write(f"dlq messages: {attrs.get('ApproximateNumberOfMessages', '0')}\n")
    got = await asyncio.to_thread(
        sqs.receive_message, QueueUrl=url, MaxNumberOfMessages=args.max, WaitTimeSeconds=0,
        VisibilityTimeout=0, AttributeNames=["ApproximateReceiveCount"],
    )  # fmt: skip
    for message in got.get("Messages") or []:
        job_id = dlq_job_id(message.get("Body"))
        if job_id is None:
            out.write("message: not a job message (poison)\n")
            continue
        item = await stores.table.get(job_key(job_id))
        if item is None:
            out.write(f"job {job_id}: unknown (no job item)\n")
            continue
        out.write(
            f"job {job_id}: kind={item.get('kind')} status={item.get('status')} "
            f"attempts={item.get('attempts')}/{item.get('max_attempts')} "
            f"last_error={item.get('last_error') or '-'}\n"
        )


def _line_buffered_output() -> None:
    """The worker is long-running and often redirected to a file: write every line as it is
    printed instead of holding it in a block buffer (as PYTHONUNBUFFERED=1 would)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(Exception):  # a replaced stream that cannot be reconfigured
                reconfigure(line_buffering=True)


def run(args: argparse.Namespace, out: TextIO) -> int:
    if args.area == "worker":
        _line_buffered_output()
        asyncio.run(_worker(args.once))
    elif args.area == "costs":
        asyncio.run(_costs(args, out))
    elif args.area == "reports":
        asyncio.run(_reports(args, out))
    elif args.area == "jobs":
        asyncio.run(_jobs(args, out))
    else:
        asyncio.run(_changes(args, out))
    return 0
