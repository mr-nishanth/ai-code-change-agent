"""Dependency requests are validated by the platform before anything is fetched (ADR-010)."""

import pytest

from ai_code_change_agent.domain.dependencies.policy import (
    DependencyError,
    DependencyFailureCode,
    DependencyPolicyError,
    DependencyRequest,
    TargetEnvironment,
    merge_two_requests,
    parse_manifest_requirement,
    parse_request,
    specifiers_compatible,
    validate_python_requirement,
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


def test_dependency_request_extras_and_cache_token() -> None:
    req = DependencyRequest(
        name="uvicorn",
        specifiers=(">=0.30",),
        extras=("standard",),
        hashes=("b" * 64, "a" * 64),
    )
    assert req.requirement() == "uvicorn[standard]>=0.30"
    assert req.cache_token() == f"uvicorn[standard]>=0.30#sha256:{'a' * 64},{'b' * 64}"
    plain = DependencyRequest("flask", ("==3.1.0",))
    assert plain.cache_token() == plain.requirement() == "flask==3.1.0"
    err = DependencyError(DependencyFailureCode.MANIFEST_INVALID, detail="bad")
    assert isinstance(err, DependencyPolicyError)
    assert str(err) == "dependency_manifest_invalid"
    assert err.code == "dependency_manifest_invalid"
    assert err.detail == "bad"


def test_parse_manifest_requirement_supports_extras_markers_and_hashes() -> None:
    env = TargetEnvironment()
    req = parse_manifest_requirement(
        'Uvicorn[Standard, http2] >= 0.30; python_version >= "3.12" and sys_platform == "linux"',
        target_env=env,
        hashes=(f"sha256:{'A' * 64}",),
    )
    assert req == DependencyRequest(
        name="uvicorn",
        specifiers=(">=0.30",),
        extras=("http2", "standard"),
        hashes=("a" * 64,),
    )
    assert (
        parse_manifest_requirement('pywin32>=300; sys_platform == "win32"', target_env=env) is None
    )
    assert parse_manifest_requirement('pyobjc; sys_platform == "darwin"', target_env=env) is None
    assert parse_manifest_requirement('tomli>=2; python_version < "3.11"', target_env=env) is None
    assert parse_manifest_requirement('pytest>=8; extra == "dev"', target_env=env) is None
    assert parse_manifest_requirement(
        'pytest>=8; extra == "dev"', target_env=env, active_extras=frozenset({"dev"})
    ) == DependencyRequest("pytest", (">=8",))


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "pkg\x00",
        "-r other.txt",
        "--index-url https://evil.invalid/simple",
        "pkg @ https://evil.invalid/pkg.whl",
        "pkg @ file:///tmp/pkg",
        "https://user:pass@evil.invalid/pkg.whl",
        "https://${TOKEN}@evil.invalid/pkg.whl",
        "git+https://github.com/org/repo.git",
        "../local/pkg",
        "./local/pkg",
        "pkg.whl",
        "pkg.tar.gz",
        "a" * 513,
    ],
)
def test_parse_manifest_requirement_rejects_unsafe_inputs(raw: str) -> None:
    with pytest.raises(DependencyPolicyError, match="dependency_manifest_invalid"):
        parse_manifest_requirement(raw)


def test_default_target_environment_maps_architecture(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_code_change_agent.domain.dependencies.policy import default_target_environment

    monkeypatch.setattr("platform.machine", lambda: "arm64")
    assert default_target_environment().platform_machine == "aarch64"
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    assert default_target_environment().platform_machine == "x86_64"


def test_parse_manifest_requirement_rejects_invalid_hashes() -> None:
    with pytest.raises(DependencyPolicyError, match="dependency_manifest_invalid"):
        parse_manifest_requirement("flask==3.1.0", hashes=("md5:deadbeef",))


def test_validate_python_requirement() -> None:
    env = TargetEnvironment()
    validate_python_requirement(None, target_env=env)
    validate_python_requirement(">=3.11", target_env=env)
    validate_python_requirement(">=3.12,<3.14", target_env=env)
    with pytest.raises(DependencyPolicyError, match="dependency_python_unsupported"):
        validate_python_requirement(">=3.13", target_env=env)
    with pytest.raises(DependencyPolicyError, match="dependency_python_unsupported"):
        validate_python_requirement("<3.12", target_env=env)
    with pytest.raises(DependencyPolicyError, match="dependency_manifest_invalid"):
        validate_python_requirement("not-a-spec", target_env=env)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ((">=2.0",), ("<3.0",), True),
        (("~=1.4.0",), (">=1.4.2",), True),
        (("==1.0.*",), ("!=1.0.1",), True),
        (("==1.0",), ("==1.0.0",), True),
        (("==1.0",), ("==2.0",), False),
        ((">=2.0",), ("<2.0",), False),
        (("~=1.4",), (">=2.0",), False),
        (("==1.0",), ("!=1.0",), False),
        ((">=1.0", "<=1.0"), ("!=1.0",), False),
        (("==1.0.*",), ("!=1.0.*",), False),
        (("===foo",), ("===bar",), False),
    ],
)
def test_specifiers_compatible(
    left: tuple[str, ...], right: tuple[str, ...], expected: bool
) -> None:
    assert specifiers_compatible(left, right) is expected


def test_merge_two_requests_combines_and_detects_conflicts() -> None:
    merged = merge_two_requests(
        DependencyRequest("httpx", (">=0.27",), extras=("http2",), hashes=("a" * 64, "b" * 64)),
        DependencyRequest("httpx", ("<0.29",), extras=("socks",), hashes=("b" * 64,)),
    )
    assert merged == DependencyRequest(
        name="httpx",
        specifiers=("<0.29", ">=0.27"),
        extras=("http2", "socks"),
        hashes=("b" * 64,),
    )
    # Pinning from one side preserves hash
    kept = merge_two_requests(
        DependencyRequest("flask", ("==3.1.0",), hashes=("c" * 64,)),
        DependencyRequest("flask", (">=3.0",)),
    )
    assert kept.hashes == ("c" * 64,)
    with pytest.raises(DependencyPolicyError, match="dependency_conflict"):
        merge_two_requests(
            DependencyRequest("flask", ("==3.0.0",)),
            DependencyRequest("flask", ("==3.1.0",)),
        )
    with pytest.raises(DependencyPolicyError, match="dependency_conflict"):
        merge_two_requests(
            DependencyRequest("flask", ("==3.1.0",), hashes=("a" * 64,)),
            DependencyRequest("flask", ("==3.1.0",), hashes=("b" * 64,)),
        )
    with pytest.raises(DependencyPolicyError, match="dependency_conflict"):
        merge_two_requests(DependencyRequest("flask"), DependencyRequest("click"))
