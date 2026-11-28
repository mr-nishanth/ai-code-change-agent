"""Test-only access to the local Floci AWS emulator (S2115).

No application code talks to Floci; these helpers exist for the `floci` tests alone. Every client is
built from an explicit boto3 Session with the dummy credentials `test`/`test`, so the developer's
AWS profile, environment credentials and config files are never consulted, and the endpoint must be
a loopback address so a test can never reach real AWS."""

import os
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import boto3  # type: ignore[import-untyped]
import httpx
from botocore.config import Config  # type: ignore[import-untyped]

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
REGION = "us-east-1"


def require_loopback(url: str) -> str:
    """Return `url` unchanged when it is an http(s) URL on a loopback host, else refuse it."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or parts.hostname not in LOOPBACK_HOSTS:
        raise ValueError(f"Floci endpoint must be a loopback http(s) URL, got {url!r}")
    return url.rstrip("/")


FLOCI_ENDPOINT = require_loopback(
    os.environ.get("ACCA_FLOCI_ENDPOINT_URL", "http://127.0.0.1:4566")
)


def floci_client(service: str) -> Any:
    session = boto3.session.Session(
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name=REGION,
    )
    return session.client(
        service,
        endpoint_url=FLOCI_ENDPOINT,
        config=Config(
            retries={"max_attempts": 2, "mode": "standard"},
            connect_timeout=3,
            read_timeout=60,
        ),
    )


def floci_reachable() -> str | None:
    """None when Floci answers its health endpoint, otherwise a skip reason."""
    url = f"{FLOCI_ENDPOINT}/_floci/health"
    try:
        response = httpx.get(url, timeout=2.0)
    except httpx.HTTPError as exc:
        return f"Floci not reachable at {FLOCI_ENDPOINT}: {type(exc).__name__}"
    if response.status_code != 200:
        return f"Floci health check at {url} returned HTTP {response.status_code}"
    return None


def unique_name(prefix: str) -> str:
    """A lowercase name that never collides with bootstrap resources or other test runs."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def wait_until[T](fn: Callable[[], T | None], timeout: float = 30.0, interval: float = 0.5) -> T:
    """Poll `fn` until it returns something other than None or False; fail at the deadline."""
    deadline = time.monotonic() + timeout
    while True:
        result = fn()
        if result is not None and result is not False:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError(f"condition not met within {timeout:.1f}s")
        time.sleep(interval)
