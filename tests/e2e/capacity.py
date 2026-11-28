"""Counts what the application asks of DynamoDB, SQS and S3 during an e2e run (owner cost request).

`install` wraps `local_session` (the one place the application builds its local boto3 session, used by
`AwsClients` and so by the API, the CLI, the worker and the checkpointer) and registers botocore
handlers on each new session: every DynamoDB data-plane call asks for `ReturnConsumedCapacity=TOTAL`
(Floci 2.5.0 returns it; measured units, not estimates) and every call is tallied by service, operation,
table role and index. Test inspection clients are built before `install` (or with
`tests.support.floci.floci_client`), so they are never counted."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

import pytest

from ai_code_change_agent.integrations.aws import clients as aws_clients

READ_OPS = frozenset({"GetItem", "Query", "Scan", "BatchGetItem", "TransactGetItems"})
WRITE_OPS = frozenset(
    {"PutItem", "UpdateItem", "DeleteItem", "BatchWriteItem", "TransactWriteItems"}
)


@dataclass
class Tally:
    calls: Counter[tuple[str, str, str, str]] = field(default_factory=Counter)
    units: defaultdict[tuple[str, str, str, str], float] = field(
        default_factory=lambda: defaultdict(float)
    )
    roles: dict[str, str] = field(default_factory=dict)  # table name -> "app" | "checkpoints"

    def _role(self, table: str | None) -> str:
        return self.roles.get(table or "", "-" if table is None else "other")

    def before(self, params: dict[str, Any], model: Any, context: dict[str, Any], **_: Any) -> None:
        if model.name in READ_OPS | WRITE_OPS:
            params.setdefault("ReturnConsumedCapacity", "TOTAL")
        tables = [params["TableName"]] if "TableName" in params else []
        tables += list(params.get("RequestItems") or {})
        tables += [
            next(iter(t.values())).get("TableName")
            for t in params.get("TransactItems") or []
            if t and isinstance(next(iter(t.values())), dict)
        ]
        context["acca_table"] = tables[0] if tables else None
        context["acca_index"] = params.get("IndexName") or "-"

    def after_dynamodb(
        self, parsed: dict[str, Any], model: Any, context: dict[str, Any], **_: Any
    ) -> None:
        key = (
            "dynamodb",
            model.name,
            self._role(context.get("acca_table")),
            context.get("acca_index", "-"),
        )
        self.calls[key] += 1
        consumed = parsed.get("ConsumedCapacity")
        for entry in consumed if isinstance(consumed, list) else [consumed] if consumed else []:
            self.units[key] += float(entry.get("CapacityUnits") or 0)

    def after_other(self, model: Any, event_name: str, **_: Any) -> None:
        service = event_name.split(".")[1]
        self.calls[(service, model.name, "-", "-")] += 1

    def summary(self) -> dict[str, Any]:
        def total(
            ops: frozenset[str], role: str | None = None, *, polling: bool | None = None
        ) -> float:
            return sum(
                u for (svc, op, r, idx), u in self.units.items()
                if svc == "dynamodb" and op in ops and (role is None or r == role)
                and (polling is None or (idx == "GSI3") == polling)
            )  # fmt: skip

        return {
            "rru": total(READ_OPS),
            "wru": total(WRITE_OPS),
            "rru_app": total(READ_OPS, "app"),
            "wru_app": total(WRITE_OPS, "app"),
            "rru_checkpoints": total(READ_OPS, "checkpoints"),
            "wru_checkpoints": total(WRITE_OPS, "checkpoints"),
            "rru_gsi3_polling": total(READ_OPS, polling=True),
            "dynamodb_requests": sum(n for k, n in self.calls.items() if k[0] == "dynamodb"),
            "sqs_requests": sum(n for k, n in self.calls.items() if k[0] == "sqs"),
            "s3_requests": sum(n for k, n in self.calls.items() if k[0] == "s3"),
        }

    def table(self) -> str:
        rows = sorted(self.calls.items())
        lines = ["| service | operation | table | index | requests | capacity units |",
                 "|---|---|---|---|---:|---:|"]  # fmt: skip
        for key, n in rows:
            units = f"{self.units[key]:g}" if key[0] == "dynamodb" else "-"
            lines.append(f"| {' | '.join(key)} | {n} | {units} |")
        return "\n".join(lines)


def install(monkeypatch: pytest.MonkeyPatch, tally: Tally) -> None:
    original = aws_clients.local_session

    def counted(region: str) -> Any:
        session = original(region)
        session.events.register("before-parameter-build.dynamodb", tally.before)
        session.events.register("after-call.dynamodb", tally.after_dynamodb)
        session.events.register("after-call.sqs", tally.after_other)
        session.events.register("after-call.s3", tally.after_other)
        return session

    monkeypatch.setattr(aws_clients, "local_session", counted)
