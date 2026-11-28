"""Smoke test: Floci answers, accepts the dummy credentials and round-trips an S3 object."""

from tests.support.floci import FLOCI_ENDPOINT, floci_client, unique_name


def test_sts_accepts_dummy_credentials(floci: str) -> None:
    assert floci == FLOCI_ENDPOINT
    identity = floci_client("sts").get_caller_identity()
    assert identity["Account"]


def test_s3_object_round_trip(floci: str) -> None:
    s3 = floci_client("s3")
    bucket = unique_name("acca-test")
    s3.create_bucket(Bucket=bucket)
    try:
        s3.put_object(Bucket=bucket, Key="hello.txt", Body=b"hello")
        body = s3.get_object(Bucket=bucket, Key="hello.txt")["Body"].read()
        assert body == b"hello"
    finally:
        s3.delete_object(Bucket=bucket, Key="hello.txt")
        s3.delete_bucket(Bucket=bucket)
