"""The table definitions agree and stay cheap (owner request: avoid over-cost; DM 6, 11).

`infra/floci/schema/*.json` is the one CreateTable input: the bootstrap creates the tables from it
and Terraform (`infra/terraform/local/main.tf`) decodes the same files. These checks run without
Floci or Terraform."""

import json
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ai_code_change_agent.application.costs.usage import LlmCallRecord
from ai_code_change_agent.domain.changes.models import CodeChangeRequest
from ai_code_change_agent.persistence.dynamodb.change_store import request_item
from ai_code_change_agent.persistence.dynamodb.job_items import new_job
from ai_code_change_agent.persistence.dynamodb.usage_store import usage_item

REPO = Path(__file__).resolve().parents[3]
SCHEMA_DIR = REPO / "infra" / "floci" / "schema"
MAIN_TF = (REPO / "infra" / "terraform" / "local" / "main.tf").read_text()
DYNAMODB_SH = (REPO / "infra" / "floci" / "init" / "60-dynamodb.sh").read_text()
NOW = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
GSI_KEYS = {f"GSI{n}{part}" for n in (1, 2, 3) for part in ("PK", "SK")}


def schema(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((SCHEMA_DIR / f"{name}.json").read_text())
    return document


def projections() -> dict[str, dict[str, Any]]:
    return {g["IndexName"]: g["Projection"] for g in schema("acca-app")["GlobalSecondaryIndexes"]}


def test_no_index_projects_all_attributes() -> None:
    """Every write to an item that changes a projected attribute also writes the index: GSI1 and
    GSI3 project keys only, GSI2 only the two audit filter attributes."""
    assert projections() == {
        "GSI1": {"ProjectionType": "KEYS_ONLY"},
        "GSI2": {"ProjectionType": "INCLUDE", "NonKeyAttributes": ["entity_type", "entity_id"]},
        "GSI3": {"ProjectionType": "KEYS_ONLY"},
    }
    assert all(p["ProjectionType"] != "ALL" for p in projections().values())


def test_both_tables_are_on_demand_standard_class() -> None:
    for name in ("acca-app", "acca-checkpoints"):
        document = schema(name)
        assert document["BillingMode"] == "PAY_PER_REQUEST"
        assert document["TableClass"] == "STANDARD"
        assert "TableName" not in document and "ProvisionedThroughput" not in document
    assert "GlobalSecondaryIndexes" not in schema("acca-checkpoints")


def test_terraform_decodes_the_same_schema_files() -> None:
    """Terraform reads every table attribute from the schema JSON, so the two cannot disagree."""
    files = set(
        re.findall(r'file\("\$\{path\.module\}/\.\./\.\./floci/schema/([\w-]+\.json)"\)', MAIN_TF)
    )
    assert files == {p.name for p in SCHEMA_DIR.glob("*.json")}
    for line in (
        "billing_mode = each.value.schema.BillingMode",
        "table_class  = each.value.schema.TableClass",
        "for_each = each.value.schema.AttributeDefinitions",
        "for_each = try(each.value.schema.GlobalSecondaryIndexes, [])",
        "projection_type    = global_secondary_index.value.Projection.ProjectionType",
        "non_key_attributes = try(global_secondary_index.value.Projection.NonKeyAttributes, null)",
    ):
        assert line in MAIN_TF, line
    assert 'projection_type    = "' not in MAIN_TF  # no literal projection overrides the file


def test_terraform_and_bootstrap_enable_the_same_ttl_attributes() -> None:
    tf_ttl = dict(
        re.findall(r'schema\s*=\s*jsondecode\(file\("[^"]*/([\w-]+)\.json"\)\)\s*'
                   r'ttl_attribute\s*=\s*"(\w+)"', MAIN_TF)
    )  # fmt: skip
    sh_ttl = dict(re.findall(r'^ensure_table "[^"]*" ([\w-]+)\.json (\w+)$', DYNAMODB_SH, re.M))
    assert tf_ttl == sh_ttl == {"acca-app": "expires_at", "acca-checkpoints": "ttl"}


def _index_keys(item: dict[str, Any]) -> set[str]:
    return GSI_KEYS & set(item)


def test_indexes_are_sparse() -> None:
    """Only items that a listing or the dispatcher must find carry index keys."""
    request = CodeChangeRequest(uuid.uuid4(), "10001", "SHOP-1", "SHOP")
    assert _index_keys(request_item(request, created_at=NOW)) == {"GSI1PK", "GSI1SK"}
    job = new_job("k.test", {}, dedupe_key="d", max_attempts=3, now=NOW)
    assert _index_keys(job.job) == {"GSI3PK", "GSI3SK"}
    assert _index_keys(job.guard) == set()
    record = LlmCallRecord(
        call_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        attempt_id=None,
        stage="plan",
        provider="anthropic",
        model="m",
        succeeded=True,
        usage=None,
        cost_usd=Decimal("0"),
        pricing_version="p",
        provider_message_id=None,
        long_context=False,
        inference_geo=None,
        error_code=None,
        created_at=NOW,
    )
    assert _index_keys(usage_item(record, "USAGE#SES#x")) == set()
