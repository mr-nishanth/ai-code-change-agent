"""Dependency requests are validated by the platform before anything is fetched (ADR-010)."""

import pytest

from ai_code_change_agent.domain.dependencies.policy import (
    DependencyPolicyError,
    parse_request,
    validate_requests,
    wheel_identity,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("fastapi", "fastapi"),
        ("Flask==3.1.0", "flask==3.1.0"),
        ("SQLAlchemy >= 2.0, <3", "sqlalchemy>=2.0,<3"),
        ("python_jose~=3.3", "python-jose~=3.3"),
        ("zope.interface===7.2", "zope-interface===7.2"),
    ],
)
def test_valid_requests_are_normalised(raw: str, expected: str) -> None:
    assert parse_request(raw).requirement() == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "git+https://github.com/x/y.git",
        "https://evil.invalid/pkg.whl",
        "pkg @ https://evil.invalid/pkg.whl",
        "../local/path",
        "./pkg",
        "pkg[extra]",
        'pkg; os_name == "nt"',
        "pkg==latest",
        "pkg --index-url https://evil.invalid",
        "-r other.txt",
        "a" * 201,
    ],
)
def test_urls_paths_extras_markers_and_options_are_refused(raw: str) -> None:
    with pytest.raises(DependencyPolicyError, match="dependency_request_invalid"):
        parse_request(raw)


def test_duplicates_after_normalisation_conflict_and_limits_apply() -> None:
    with pytest.raises(DependencyPolicyError, match="dependency_conflict"):
        validate_requests(["Foo_Bar==1", "foo-bar==2"], max_packages=5)
    with pytest.raises(DependencyPolicyError, match="dependency_limit_exceeded"):
        validate_requests(["a", "b", "c"], max_packages=2)
    assert [r.name for r in validate_requests(["zz", "aa"], max_packages=5)] == ["aa", "zz"]
    assert validate_requests([], max_packages=5) == ()


@pytest.mark.parametrize(
    ("filename", "identity"),
    [
        ("fastapi-0.165.0-py3-none-any.whl", ("fastapi", "0.165.0")),
        (
            "pydantic_core-2.50.5-cp312-cp312-manylinux_2_17_aarch64.manylinux2014_aarch64.whl",
            ("pydantic-core", "2.50.5"),
        ),
        ("pkg-1.0-1build-py3-none-any.whl", ("pkg", "1.0")),
    ],
)
def test_wheel_identity(filename: str, identity: tuple[str, str]) -> None:
    assert wheel_identity(filename) == identity


@pytest.mark.parametrize(
    "filename", ["pkg-1.0.tar.gz", "pkg-1.0.zip", "pkg.whl", "../x-1-py3-none-any.whl"]
)
def test_source_distributions_and_odd_files_are_not_wheels(filename: str) -> None:
    with pytest.raises(DependencyPolicyError, match="dependency_wheel_unavailable"):
        wheel_identity(filename)
