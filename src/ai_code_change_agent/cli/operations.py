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

from ai_code_change_agent.cli.stores import CliStores
from ai_code_change_agent.composition import build_cost_reporter, worker_app
from ai_code_change_agent.core.config import get_settings
from ai_code_change_agent.domain.changes.models import ApprovalDecision, ApprovalGate
from ai_code_change_agent.orchestration.intake import RESUME


def add_parsers(top: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    worker = top.add_parser("worker", help="run the job worker (trusted host process)")
    worker.add_argument("--once", action="store_true", help="process at most one job and exit")
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


def _actor() -> str:
    name = re.sub(r"[^A-Za-z0-9_.@-]", "_", getpass.getuser())[:64] or "unknown"
    return f"cli:{name}"


async def _worker(once: bool) -> None:
    async with worker_app(get_settings()) as app:
        try:
            if once:
                await app.worker.run_once()
                return
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, stop.set)
            await app.worker.run(stop)
        finally:
            await app.costs.print_session_summary()  # recomputed from the persisted ledger


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
                 "actor": a.actor, "actor_verified": a.actor_verified}
                for a in approvals
            ],
        }  # fmt: skip
        out.write(json.dumps(data, indent=2) + "\n")
        return
    decision = ApprovalDecision.APPROVED if args.command == "approve" else ApprovalDecision.REJECTED
    approval, attempt = await service.decide(
        args.attempt, ApprovalGate(args.gate), args.sha256, decision, _actor()
    )
    # the workflow re-reads and re-checks the approval itself; this only wakes it
    await stores.job_queue().enqueue(
        RESUME, {"attempt_id": str(attempt.id)},
        dedupe_key=f"{RESUME}:{attempt.id}:{approval.id}",
        max_attempts=stores.settings.job_max_attempts,
    )  # fmt: skip
    out.write(
        f"{decision.value} {args.gate} for attempt {attempt.id}; status {attempt.status.value}\n"
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
    else:
        asyncio.run(_changes(args, out))
    return 0
