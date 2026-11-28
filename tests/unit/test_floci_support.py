import pytest

from tests.support.floci import require_loopback, unique_name, wait_until


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:4566", "http://localhost:4566/", "http://[::1]:4566"]
)
def test_loopback_endpoints_are_accepted(url: str) -> None:
    assert require_loopback(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "https://s3.amazonaws.com",
        "http://sts.us-east-1.amazonaws.com",
        "http://10.0.0.5:4566",
        "http://127.0.0.1.example.com:4566",
        "file:///tmp/x",
    ],
)
def test_non_loopback_endpoints_are_refused(url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        require_loopback(url)


def test_unique_names_differ() -> None:
    assert unique_name("t") != unique_name("t")
    assert unique_name("acca").startswith("acca-")


def test_wait_until_returns_value_and_times_out() -> None:
    calls = iter([None, False, "ok"])
    assert wait_until(lambda: next(calls), timeout=5, interval=0) == "ok"
    with pytest.raises(TimeoutError):
        wait_until(lambda: None, timeout=0.05, interval=0.01)
