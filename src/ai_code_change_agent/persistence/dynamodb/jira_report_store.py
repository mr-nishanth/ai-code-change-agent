"""DynamoDB store for durable Jira reports (`application.jira.reports`, DYNAMODB-DATA-MODEL.md).

One item per report: `JIRAREPORT#<report_id>/REPORT` with the posted text, its sha256, `state`
(PENDING | IN_PROGRESS | POSTED | FAILED_PERMANENT), `attempts` (delivery rounds), `next_retry_at`,
`last_error` (a code), `comment_id`, `claimed_at` + `claim_token` (the round in flight) and
`row_version`. While not settled it sits in the sparse GSI3 partition `REPORTDUE` with
`GSI3SK = <due>#<id>` (due = next_retry_at, or claimed_at + stale for a claim in flight), which
the worker's dispatcher queries for retries; GSI2 `LIST#JIRAREPORT` lists every report newest
first for the operator CLI. A settled report leaves GSI3 and gets `expires_at` (TTL). Every write
is conditional; failures are `PersistenceError` without driver text (`table`)."""

import uuid
from datetime import datetime, timedelta
from typing import Any

from ai_code_change_agent.application.jira.reports import ReportClaim, ReportRecord, ReportState
from ai_code_change_agent.persistence.dynamodb import keys
from ai_code_change_agent.persistence.dynamodb.clock import Clock, SystemClock
from ai_code_change_agent.persistence.dynamodb.codec import (
    decode_enum,
    decode_int,
    decode_timestamp,
    encode_epoch_seconds,
    encode_timestamp,
)
from ai_code_change_agent.persistence.dynamodb.errors import Item
from ai_code_change_agent.persistence.dynamodb.table import Table

RETENTION_DAYS = 90
_N = {"#state": "state"}


class _Refused(Exception):  # noqa: N818 - internal signal: a condition failed
    pass


def _ts(value: object) -> datetime | None:
    return decode_timestamp(value) if value is not None else None


def _record(item: Item) -> ReportRecord:
    return ReportRecord(
        report_id=str(item.get("report_id")),
        issue_key=str(item.get("issue_key")),
        text=str(item.get("text") or ""),
        content_sha256=str(item.get("content_sha256") or ""),
        state=decode_enum(ReportState, item.get("state"), "state"),
        attempts=decode_int(item.get("attempts"), "attempts"),
        next_retry_at=_ts(item.get("next_retry_at")),
        last_error=item.get("last_error"),
        comment_id=item.get("comment_id"),
        created_at=_ts(item.get("created_at")),
        updated_at=_ts(item.get("updated_at")),
    )


class DynamoJiraReportStore:
    def __init__(self, table: Table, *, clock: Clock | None = None) -> None:
        self._table = table
        self._clock: Clock = clock or SystemClock()

    async def create(self, report_id: str, issue_key: str, text: str, sha256: str) -> bool:
        now = self._clock.now()
        item: dict[str, Any] = {
            **keys.jira_report(report_id).as_dict(),
            **keys.jira_report_gsi3(now, report_id),
            **keys.jira_report_gsi2(now, report_id),
            "report_id": report_id,
            "issue_key": issue_key,
            "text": text,
            "content_sha256": sha256,
            "state": ReportState.PENDING.value,
            "attempts": 0,
            "next_retry_at": encode_timestamp(now),
            "created_at": encode_timestamp(now),
            "updated_at": encode_timestamp(now),
            "row_version": 1,
        }
        try:
            await self._table.put(item, condition="attribute_not_exists(PK)", on_failed=_Refused())
        except _Refused:
            return False
        return True

    async def claim(self, report_id: str, stale_seconds: int) -> ReportClaim | None:
        now = self._clock.now()
        token = uuid.uuid4().hex
        # due again (for the dispatcher) once this claim is stale: a crashed poster is recovered
        due = keys.jira_report_gsi3(now + timedelta(seconds=stale_seconds), report_id)
        try:
            old = await self._table.update(
                keys.jira_report(report_id),
                update=(
                    "SET #state = :ip, claimed_at = :now, claim_token = :token, GSI3SK = :due, "
                    "updated_at = :now ADD attempts :one, row_version :one"
                ),
                condition=(
                    "attribute_exists(PK) AND ((#state = :pending AND next_retry_at <= :now) "
                    "OR (#state = :ip AND claimed_at < :stale))"
                ),
                names=_N,
                values={
                    ":ip": ReportState.IN_PROGRESS.value,
                    ":pending": ReportState.PENDING.value,
                    ":now": encode_timestamp(now),
                    ":stale": encode_timestamp(now - timedelta(seconds=stale_seconds)),
                    ":token": token,
                    ":due": due["GSI3SK"],
                    ":one": 1,
                },
                on_failed=_Refused(),
                return_values="ALL_OLD",
            )
        except _Refused:
            return None
        record = _record(old)
        return ReportClaim(record, record.attempts + 1, token)

    async def finish(
        self,
        report_id: str,
        token: str,
        state: ReportState,
        *,
        next_retry_at: datetime | None = None,
        error: str | None = None,
        comment_id: str | None = None,
    ) -> bool:
        now = self._clock.now()
        sets = ["#state = :state", "updated_at = :now"]
        removes = ["claim_token", "claimed_at"]
        values: dict[str, Any] = {
            ":state": ReportState(state).value,
            ":ip": ReportState.IN_PROGRESS.value,
            ":token": token,
            ":now": encode_timestamp(now),
            ":one": 1,
        }
        if error is not None:
            sets.append("last_error = :error")
            values[":error"] = error
        else:
            removes.append("last_error")
        if comment_id is not None:
            sets.append("comment_id = :comment")
            values[":comment"] = comment_id
        if state is ReportState.PENDING:
            due = next_retry_at or now
            sets += ["next_retry_at = :next", "GSI3SK = :due"]
            values[":next"] = encode_timestamp(due)
            values[":due"] = keys.jira_report_gsi3(due, report_id)["GSI3SK"]
        else:  # settled: out of the retry index, kept for RETENTION_DAYS
            sets.append("expires_at = :ttl")
            values[":ttl"] = encode_epoch_seconds(now + timedelta(days=RETENTION_DAYS))
            removes += ["GSI3PK", "GSI3SK", "next_retry_at"]
        try:
            await self._table.update(
                keys.jira_report(report_id),
                update="SET "
                + ", ".join(sets)
                + " ADD row_version :one REMOVE "
                + ", ".join(removes),
                condition="#state = :ip AND claim_token = :token",
                names=_N,
                values=values,
                on_failed=_Refused(),
                return_values="NONE",
            )
        except _Refused:
            return False
        return True

    async def due(self, limit: int) -> list[str]:
        now = self._clock.now()
        rows = await self._table.query(
            key_condition="GSI3PK = :p AND GSI3SK <= :bound",
            values={":p": keys.GSI_REPORT, ":bound": encode_timestamp(now) + "#~"},
            index="GSI3",
            limit=limit,
            page_size=limit,
        )
        out: list[str] = []
        for row in rows:
            pk = row.get("PK")
            if isinstance(pk, str) and pk.startswith("JIRAREPORT#"):
                out.append(pk.removeprefix("JIRAREPORT#"))
        return out

    async def get(self, report_id: str) -> ReportRecord | None:
        item = await self._table.get(keys.jira_report(report_id))
        return _record(item) if item is not None else None

    async def recent(self, limit: int) -> list[ReportRecord]:
        rows = await self._table.query(
            key_condition="GSI2PK = :p",
            values={":p": keys.GSI_LIST_REPORT},
            index="GSI2",
            forward=False,
            limit=limit,
            page_size=limit,
        )
        return [_record(item) for item in await self._table.fetch(rows, consistent=False)]


__all__ = ["DynamoJiraReportStore"]
