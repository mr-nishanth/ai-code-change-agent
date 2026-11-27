"""Floci API-level coverage for STS/IAM, S3, SQS (DLQ redrive), Secrets Manager and CloudWatch.

Every test creates uniquely named resources against the local emulator and deletes them in `finally`.
IAM enforcement is off by default in Floci, so only the IAM API surface is asserted, never authorisation."""

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from tests.support.floci import floci_client, unique_name, wait_until

pytestmark = [pytest.mark.floci, pytest.mark.integration]

ASSUME_ROLE_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}
INLINE_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}],
}


def _as_dict(document: Any) -> Any:
    """IAM returns policy documents either decoded or as (URL-encoded) JSON text."""
    if isinstance(document, str):
        return json.loads(unquote(document))
    return document


def test_sts_identity_and_iam_role_with_inline_policy(floci: str) -> None:
    assert floci_client("sts").get_caller_identity()["Account"] == "000000000000"

    iam = floci_client("iam")
    role = unique_name("acca-role")
    policy = "acca-inline"
    iam.create_role(RoleName=role, AssumeRolePolicyDocument=json.dumps(ASSUME_ROLE_POLICY))
    try:
        iam.put_role_policy(
            RoleName=role, PolicyName=policy, PolicyDocument=json.dumps(INLINE_POLICY)
        )
        fetched = iam.get_role(RoleName=role)["Role"]
        assert fetched["RoleName"] == role
        assert fetched["Arn"].endswith(f":role/{role}")
        assert _as_dict(fetched["AssumeRolePolicyDocument"]) == ASSUME_ROLE_POLICY
        assert iam.list_role_policies(RoleName=role)["PolicyNames"] == [policy]
        inline = iam.get_role_policy(RoleName=role, PolicyName=policy)
        assert _as_dict(inline["PolicyDocument"]) == INLINE_POLICY
    finally:
        try:
            iam.delete_role_policy(RoleName=role, PolicyName=policy)
        finally:
            iam.delete_role(RoleName=role)


def test_s3_object_lifecycle_and_presigned_get(floci: str) -> None:
    s3 = floci_client("s3")
    bucket = unique_name("acca-s3")
    key = "reports/result.json"
    body = json.dumps({"run": uuid.uuid4().hex}).encode()
    s3.create_bucket(Bucket=bucket)
    try:
        put = s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
        head = s3.head_object(Bucket=bucket, Key=key)
        assert head["ETag"] == put["ETag"]
        assert head["ContentLength"] == len(body)
        assert s3.get_object(Bucket=bucket, Key=key)["Body"].read() == body
        listed = s3.list_objects_v2(Bucket=bucket, Prefix="reports/")
        assert [item["Key"] for item in listed.get("Contents", [])] == [key]

        url = s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=60
        )
        response = httpx.get(url, timeout=10.0)
        assert response.status_code == 200
        assert response.content == body
    finally:
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        finally:
            s3.delete_bucket(Bucket=bucket)


def test_sqs_dead_letter_redrive_and_move_back(floci: str) -> None:
    sqs = floci_client("sqs")
    dlq_url = sqs.create_queue(QueueName=unique_name("acca-dlq"))["QueueUrl"]
    main_url: str | None = None
    try:
        dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])[
            "Attributes"
        ]["QueueArn"]
        redrive = json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "2"})
        main_url = sqs.create_queue(
            QueueName=unique_name("acca-main"),
            Attributes={"VisibilityTimeout": "1", "RedrivePolicy": redrive},
        )["QueueUrl"]
        main_arn = sqs.get_queue_attributes(QueueUrl=main_url, AttributeNames=["QueueArn"])[
            "Attributes"
        ]["QueueArn"]
        payload = f"job-{uuid.uuid4().hex}"
        sqs.send_message(QueueUrl=main_url, MessageBody=payload)

        for _ in range(
            2
        ):  # received but never deleted: each receive counts towards maxReceiveCount
            received = wait_until(
                lambda: sqs.receive_message(QueueUrl=main_url, WaitTimeSeconds=1).get("Messages"),
                timeout=15,
            )
            assert received[0]["Body"] == payload
            time.sleep(1.5)  # past the 1 s visibility timeout

        def in_dlq() -> list[dict[str, Any]] | None:
            # A further receive on the source is what moves an over-received message to the DLQ.
            assert not sqs.receive_message(QueueUrl=main_url, WaitTimeSeconds=0).get("Messages")
            messages: list[dict[str, Any]] | None = sqs.receive_message(
                QueueUrl=dlq_url, VisibilityTimeout=0, WaitTimeSeconds=1
            ).get("Messages")
            return messages

        dead = wait_until(in_dlq, timeout=20)
        assert [message["Body"] for message in dead] == [payload]
        assert not sqs.receive_message(QueueUrl=main_url, WaitTimeSeconds=1).get("Messages")

        task = sqs.start_message_move_task(SourceArn=dlq_arn, DestinationArn=main_arn)
        assert task["TaskHandle"]
        back = wait_until(
            lambda: sqs.receive_message(QueueUrl=main_url, WaitTimeSeconds=1).get("Messages"),
            timeout=20,
        )
        assert [message["Body"] for message in back] == [payload]
        sqs.delete_message(QueueUrl=main_url, ReceiptHandle=back[0]["ReceiptHandle"])
    finally:
        try:
            if main_url is not None:
                sqs.delete_queue(QueueUrl=main_url)
        finally:
            sqs.delete_queue(QueueUrl=dlq_url)


def test_secrets_manager_versions(floci: str) -> None:
    sm = floci_client("secretsmanager")
    name = unique_name("acca/secret")
    created = sm.create_secret(Name=name, SecretString="first-value")
    try:
        first = sm.get_secret_value(SecretId=name)
        assert first["SecretString"] == "first-value"
        assert first["VersionId"] == created["VersionId"]
        assert "AWSCURRENT" in first["VersionStages"]

        updated = sm.put_secret_value(SecretId=name, SecretString="second-value")
        assert updated["VersionId"] != created["VersionId"]
        assert "AWSCURRENT" in updated["VersionStages"]
        current = sm.get_secret_value(SecretId=name)
        assert current["SecretString"] == "second-value"
        assert current["VersionId"] == updated["VersionId"]

        described = sm.describe_secret(SecretId=name)
        assert described["Name"] == name
        assert described["ARN"] == created["ARN"]
        assert "AWSCURRENT" in described["VersionIdsToStages"][updated["VersionId"]]
    finally:
        sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)


def test_cloudwatch_logs_put_get_and_filter(floci: str) -> None:
    logs = floci_client("logs")
    group = f"/acca/s2115/{uuid.uuid4().hex}"
    stream = "worker-1"
    logs.create_log_group(logGroupName=group)
    try:
        logs.create_log_stream(logGroupName=group, logStreamName=stream)
        now_ms = int(time.time() * 1000)
        logs.put_log_events(
            logGroupName=group,
            logStreamName=stream,
            logEvents=[
                {"timestamp": now_ms, "message": "job started"},
                {"timestamp": now_ms + 1, "message": "job failed: timeout"},
            ],
        )
        events = logs.get_log_events(logGroupName=group, logStreamName=stream, startFromHead=True)[
            "events"
        ]
        assert [event["message"] for event in events] == ["job started", "job failed: timeout"]

        filtered = wait_until(
            lambda: logs.filter_log_events(logGroupName=group, filterPattern="failed")["events"],
            timeout=15,
        )
        assert [event["message"] for event in filtered] == ["job failed: timeout"]
    finally:
        logs.delete_log_group(logGroupName=group)


def test_cloudwatch_metrics_put_query_and_list(floci: str) -> None:
    cw = floci_client("cloudwatch")
    namespace = f"ACCA/S2115/{uuid.uuid4().hex}"
    metric = "JobsCompleted"
    dimensions = [{"Name": "Queue", "Value": "default"}]
    now = datetime.now(UTC).replace(microsecond=0)
    cw.put_metric_data(
        Namespace=namespace,
        MetricData=[
            {
                "MetricName": metric,
                "Dimensions": dimensions,
                "Timestamp": now - timedelta(seconds=offset),
                "Value": value,
                "Unit": "Count",
            }
            for offset, value in ((0, 1.0), (1, 2.0), (2, 3.0))
        ],
    )
    start, end = now - timedelta(minutes=5), now + timedelta(minutes=5)

    def statistics_sum() -> float | None:
        points = cw.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric,
            Dimensions=dimensions,
            StartTime=start,
            EndTime=end,
            Period=600,
            Statistics=["Sum"],
        )["Datapoints"]
        total = sum(point["Sum"] for point in points)
        return total if total == 6.0 else None

    assert wait_until(statistics_sum, timeout=20) == 6.0

    def data_sum() -> float | None:
        results = cw.get_metric_data(
            MetricDataQueries=[
                {
                    "Id": "jobs",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": namespace,
                            "MetricName": metric,
                            "Dimensions": dimensions,
                        },
                        "Period": 600,
                        "Stat": "Sum",
                    },
                }
            ],
            StartTime=start,
            EndTime=end,
        )["MetricDataResults"]
        total = sum(results[0]["Values"]) if results else 0.0
        return total if total == 6.0 else None

    assert wait_until(data_sum, timeout=20) == 6.0

    listed = cw.list_metrics(Namespace=namespace)["Metrics"]
    assert [(item["MetricName"], item["Dimensions"]) for item in listed] == [(metric, dimensions)]
