"""Safe repository dependency manifest discovery, precedence, categories and security checks."""

import os
from pathlib import Path

import pytest

from ai_code_change_agent.domain.dependencies.manifests import (
    MAX_INCLUDE_DEPTH,
    discover_dependencies_from_files,
    discover_repository_dependencies,
    merge_dependency_requests,
)
from ai_code_change_agent.domain.dependencies.policy import (
    DependencyPolicyError,
    DependencyRequest,
    TargetEnvironment,
)

H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64
H4 = "4" * 64
LINUX_X86 = TargetEnvironment(platform_machine="x86_64")


def _uv_lock_base(
    *,
    root_source: str = '{ editable = "." }',
    pkg_source: str = '{ registry = "https://pypi.org/simple" }',
    wheels: str = f'[{{ url = "https://files.pythonhosted.org/flask-3.1.0-py3-none-any.whl", hash = "sha256:{H1}" }}]',
    sdist: str = "",
    extra_packages: str = "",
    version_line: str = "version = 1",
    requires_python: str = 'requires-python = ">=3.12"',
    top_markers: str = "",
    dev_deps: str = "",
) -> str:
    sdist_line = f"\nsdist = {sdist}" if sdist else ""
    markers_line = f"\nresolution-markers = {top_markers}" if top_markers else ""
    dev_block = f"\n[package.dev-dependencies]\n{dev_deps}" if dev_deps else ""
    return f"""{version_line}
revision = 3
{requires_python}{markers_line}

[[package]]
name = "demo-app"
version = "0.1.0"
source = {root_source}
dependencies = [
    {{ name = "flask" }},
]{dev_block}

[[package]]
name = "flask"
version = "3.1.0"
source = {pkg_source}{sdist_line}
wheels = {wheels}
{extra_packages}
"""


def test_precedence_uv_lock_over_pyproject_and_requirements(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("click==8.1.7\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo-app"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12"\ndependencies = ["flask>=3.0"]\n'
    )
    (tmp_path / "uv.lock").write_text(_uv_lock_base())

    res = discover_repository_dependencies(tmp_path, target_env=LINUX_X86)
    assert res.source_kind == "uv.lock"
    assert res.sources == ("uv.lock", "pyproject.toml")
    assert res.requires_python == ">=3.12"
    assert res.provisioned == (DependencyRequest("flask", ("==3.1.0",), hashes=(H1,)),)


def test_tool_only_pyproject_falls_through_to_requirements(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff]\nline-length = 100\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    )
    (tmp_path / "requirements.txt").write_text("httpx==0.28.1\n")
    (tmp_path / "requirements-dev.txt").write_text("pytest>=8.3\n")

    res = discover_repository_dependencies(tmp_path, target_env=LINUX_X86)
    assert res.source_kind == "requirements.txt"
    assert res.sources == ("requirements.txt", "requirements-dev.txt")
    assert [r.requirement() for r in res.categories.runtime] == ["httpx==0.28.1"]
    assert [r.requirement() for r in res.categories.test_dev] == ["pytest>=8.3"]
    assert [r.requirement() for r in res.provisioned] == ["httpx==0.28.1", "pytest>=8.3"]


def test_no_manifest_returns_none_gracefully(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Empty python repo\n")
    res = discover_repository_dependencies(tmp_path, target_env=LINUX_X86)
    assert res.source_kind == "none"
    assert res.sources == ()
    assert res.provisioned == ()


@pytest.mark.parametrize(
    "legacy",
    ["setup.py", "setup.cfg", "Pipfile", "Pipfile.lock", "poetry.lock"],
)
def test_legacy_only_manifests_fail_closed(tmp_path: Path, legacy: str) -> None:
    (tmp_path / legacy).write_text("name = 'legacy'\n")
    with pytest.raises(DependencyPolicyError, match="dependency_manifest_invalid"):
        discover_repository_dependencies(tmp_path, target_env=LINUX_X86)


def test_uv_lock_categories_extras_transitive_and_markers(tmp_path: Path) -> None:
    lock = f"""version = 1
requires-python = ">=3.12"

[[package]]
name = "demo-app"
version = "0.1.0"
source = {{ virtual = "." }}
dependencies = [
    {{ name = "uvicorn", extra = ["standard"] }},
    {{ name = "pywin32", marker = "sys_platform == 'win32'" }},
]

[package.dev-dependencies]
dev = [
    {{ name = "pytest" }},
]

[package.optional-dependencies]
docs = [
    {{ name = "mkdocs" }},
]

[[package]]
name = "uvicorn"
version = "0.34.0"
source = {{ registry = "https://pypi.org/simple" }}
dependencies = [{{ name = "click" }}]
wheels = [{{ url = "https://files.pythonhosted.org/uvicorn-0.34.0-py3-none-any.whl", hash = "sha256:{H1}" }}]

[package.optional-dependencies]
standard = [{{ name = "uvloop", marker = "sys_platform != 'win32'" }}]

[[package]]
name = "click"
version = "8.1.8"
source = {{ registry = "https://pypi.org/simple" }}
wheels = [{{ url = "https://files.pythonhosted.org/click-8.1.8-py3-none-any.whl", hash = "sha256:{H2}" }}]

[[package]]
name = "uvloop"
version = "0.21.0"
source = {{ registry = "https://pypi.org/simple" }}
wheels = [
    {{ url = "https://files.pythonhosted.org/uvloop-0.21.0-cp312-cp312-manylinux_2_17_x86_64.whl", hash = "sha256:{H3}" }},
    {{ url = "https://files.pythonhosted.org/uvloop-0.21.0-cp312-cp312-macosx_11_0_arm64.whl", hash = "sha256:{H4}" }},
]

[[package]]
name = "pytest"
version = "8.3.4"
source = {{ registry = "https://pypi.org/simple" }}
wheels = [{{ url = "https://files.pythonhosted.org/pytest-8.3.4-py3-none-any.whl", hash = "sha256:{H2}" }}]

[[package]]
name = "mkdocs"
version = "1.6.1"
source = {{ registry = "https://pypi.org/simple" }}
wheels = [{{ url = "https://files.pythonhosted.org/mkdocs-1.6.1-py3-none-any.whl", hash = "sha256:{H4}" }}]
"""
    (tmp_path / "uv.lock").write_text(lock)
    res = discover_repository_dependencies(tmp_path, target_env=LINUX_X86)
    assert [r.name for r in res.categories.runtime] == ["click", "uvicorn", "uvloop"]
    assert [r.name for r in res.categories.test_dev] == ["pytest"]
    assert [r.name for r in res.categories.optional["docs"]] == ["mkdocs"]
    assert [r.name for r in res.provisioned] == ["click", "pytest", "uvicorn", "uvloop"]
    uvloop_req = next(r for r in res.provisioned if r.name == "uvloop")
    assert uvloop_req.hashes == (H3,)


def test_uv_lock_multi_version_resolution_markers(tmp_path: Path) -> None:
    lock = f"""version = 1
requires-python = ">=3.12"

[[package]]
name = "demo-app"
version = "0.1.0"
source = {{ editable = "." }}
dependencies = [{{ name = "cryptography" }}]

[[package]]
name = "cryptography"
version = "48.0.1"
source = {{ registry = "https://pypi.org/simple" }}
resolution-markers = ["sys_platform == 'darwin'"]
wheels = [{{ url = "https://files.pythonhosted.org/cryptography-48.0.1-cp38-abi3-macosx_10_12_universal2.whl", hash = "sha256:{H1}" }}]

[[package]]
name = "cryptography"
version = "50.0.2"
source = {{ registry = "https://pypi.org/simple" }}
resolution-markers = ["sys_platform != 'darwin'"]
wheels = [{{ url = "https://files.pythonhosted.org/cryptography-50.0.2-cp38-abi3-manylinux_2_28_x86_64.whl", hash = "sha256:{H2}" }}]
"""
    (tmp_path / "uv.lock").write_text(lock)
    res = discover_repository_dependencies(tmp_path, target_env=LINUX_X86)
    assert res.provisioned == (DependencyRequest("cryptography", ("==50.0.2",), hashes=(H2,)),)


def test_uv_lock_rejections_and_platform_python_checks(tmp_path: Path) -> None:
    # Sdist only -> wheel unavailable
    (tmp_path / "uv.lock").write_text(
        _uv_lock_base(
            wheels="[]",
            sdist=f'{{ url = "https://files.pythonhosted.org/flask-3.1.0.tar.gz", hash = "sha256:{H1}" }}',
        )
    )
    with pytest.raises(DependencyPolicyError, match="dependency_wheel_unavailable"):
        discover_repository_dependencies(tmp_path, target_env=LINUX_X86)

    # MacOS-only wheel -> platform unsupported
    (tmp_path / "uv.lock").write_text(
        _uv_lock_base(
            wheels=f'[{{ url = "https://files.pythonhosted.org/flask-3.1.0-cp312-cp312-macosx_11_0_arm64.whl", hash = "sha256:{H1}" }}]'
        )
    )
    with pytest.raises(DependencyPolicyError, match="dependency_platform_unsupported"):
        discover_repository_dependencies(tmp_path, target_env=LINUX_X86)

    # Linux cp313-only wheel -> python unsupported
    (tmp_path / "uv.lock").write_text(
        _uv_lock_base(
            wheels=f'[{{ url = "https://files.pythonhosted.org/flask-3.1.0-cp313-cp313-manylinux_2_17_x86_64.whl", hash = "sha256:{H1}" }}]'
        )
    )
    with pytest.raises(DependencyPolicyError, match="dependency_python_unsupported"):
        discover_repository_dependencies(tmp_path, target_env=LINUX_X86)

    # Unsafe git source
    (tmp_path / "uv.lock").write_text(
        _uv_lock_base(pkg_source='{ git = "https://github.com/pallets/flask.git" }')
    )
    with pytest.raises(DependencyPolicyError, match="dependency_manifest_invalid"):
        discover_repository_dependencies(tmp_path, target_env=LINUX_X86)

    # HTTP registry URL or credentials
    (tmp_path / "uv.lock").write_text(
        _uv_lock_base(pkg_source='{ registry = "http://pypi.org/simple" }')
    )
    with pytest.raises(DependencyPolicyError, match="dependency_manifest_invalid"):
        discover_repository_dependencies(tmp_path, target_env=LINUX_X86)

    # Expected index mismatch
    (tmp_path / "uv.lock").write_text(_uv_lock_base())
    with pytest.raises(DependencyPolicyError, match="dependency_manifest_invalid"):
        discover_repository_dependencies(
            tmp_path, target_env=LINUX_X86, expected_index_url="https://internal.example/simple"
        )

    # Unsupported lockfile version
    (tmp_path / "uv.lock").write_text(_uv_lock_base(version_line="version = 2"))
    with pytest.raises(DependencyPolicyError, match="dependency_manifest_invalid"):
        discover_repository_dependencies(tmp_path, target_env=LINUX_X86)

    # Top-level resolution-markers mismatch
    (tmp_path / "uv.lock").write_text(_uv_lock_base(top_markers="[\"sys_platform == 'darwin'\"]"))
    with pytest.raises(DependencyPolicyError, match="dependency_platform_unsupported"):
        discover_repository_dependencies(tmp_path, target_env=LINUX_X86)


def test_uv_lock_stale_against_pyproject_fails(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text(_uv_lock_base())
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo-app"\nversion = "0.1.0"\n'
        'dependencies = ["flask>=3.0", "httpx>=0.28"]\n'
    )
    with pytest.raises(DependencyPolicyError, match="dependency_manifest_invalid"):
        discover_repository_dependencies(tmp_path, target_env=LINUX_X86)


def test_pyproject_categories_pep735_build_system_and_self_extras(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[project]
name = "My_Service"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "FastAPI>=0.115",
    "uvicorn[standard]>=0.30",
    "pywin32>=300; sys_platform == 'win32'",
]

[project.optional-dependencies]
lint = ["ruff>=0.6"]
dev = ["pytest>=8.3", "my-service[lint]"]
docs = ["mkdocs>=1.6"]

[dependency-groups]
typing = ["mypy>=1.11"]
test = [{ include-group = "typing" }, "pytest-cov>=5.0"]
extras = ["sphinx>=8.0"]
"""
    )
    res = discover_repository_dependencies(tmp_path, target_env=LINUX_X86)
    assert res.source_kind == "pyproject.toml"
    assert res.build_system == ("hatchling>=1.25",)
    assert [r.requirement() for r in res.categories.runtime] == [
        "fastapi>=0.115",
        "uvicorn[standard]>=0.30",
    ]
    assert [r.requirement() for r in res.categories.test_dev] == [
        "mypy>=1.11",
        "pytest>=8.3",
        "pytest-cov>=5.0",
        "ruff>=0.6",
    ]
    assert [r.requirement() for r in res.categories.optional["docs"]] == ["mkdocs>=1.6"]
    assert [r.requirement() for r in res.categories.optional["extras"]] == ["sphinx>=8.0"]
    # build-system and optional ("docs", "extras") are excluded from provisioned
    assert [r.name for r in res.provisioned] == [
        "fastapi",
        "mypy",
        "pytest",
        "pytest-cov",
        "ruff",
        "uvicorn",
    ]


@pytest.mark.parametrize(
    ("toml_text", "expected_code"),
    [
        ('[tool.poetry]\nname = "legacy"\n', "dependency_manifest_invalid"),
        (
            '[project]\nname = "x"\nrequires-python = ">=3.13"\ndependencies = []\n',
            "dependency_python_unsupported",
        ),
        (
            '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
            'backend-path = ["."]\n[project]\nname = "x"\n',
            "dependency_manifest_invalid",
        ),
        (
            '[build-system]\nrequires = ["custom"]\nbuild-backend = "evil.backend:build"\n'
            '[project]\nname = "x"\n',
            "dependency_manifest_invalid",
        ),
        (
            '[project]\nname = "x"\n[tool.uv]\nindex-url = "https://evil.invalid/simple"\n',
            "dependency_manifest_invalid",
        ),
        (
            '[project]\nname = "x"\n[tool.uv.sources]\nfoo = { git = "https://evil.invalid/x.git" }\n',
            "dependency_manifest_invalid",
        ),
        (
            '[project]\nname = "x"\n[tool.uv]\nrequired-environments = ["sys_platform == \'darwin\'"]\n',
            "dependency_platform_unsupported",
        ),
        (
            '[project]\nname = "x"\n[dependency-groups]\na = [{ include-group = "b" }]\nb = [{ include-group = "a" }]\n',
            "dependency_manifest_invalid",
        ),
        (
            '[project]\nname = "x"\n[dependency-groups]\na = [{ include-group = "missing" }]\n',
            "dependency_manifest_invalid",
        ),
        (
            '[project]\nname = "x"\n[project.optional-dependencies]\na = ["x[b]"]\nb = ["x[a]"]\n',
            "dependency_manifest_invalid",
        ),
    ],
)
def test_pyproject_rejections(tmp_path: Path, toml_text: str, expected_code: str) -> None:
    (tmp_path / "pyproject.toml").write_text(toml_text)
    with pytest.raises(DependencyPolicyError, match=expected_code):
        discover_repository_dependencies(tmp_path, target_env=LINUX_X86)


def test_requirements_continuations_comments_hashes_and_includes(tmp_path: Path) -> None:
    (tmp_path / "reqs").mkdir()
    (tmp_path / "reqs" / "base.txt").write_text(
        f"# base dependencies\nflask==3.1.0 \\\n  --hash=sha256:{H1} # web framework\n"
    )
    (tmp_path / "requirements.txt").write_text(
        "-r reqs/base.txt\n"
        'uvicorn[standard]>=0.30; python_version >= "3.12" # asgi\n'
        'pywin32>=300; sys_platform == "win32"\n'
    )
    (tmp_path / "test-requirements.txt").write_text("pytest>=8.3\n")

    res = discover_repository_dependencies(tmp_path, target_env=LINUX_X86)
    assert res.source_kind == "requirements.txt"
    assert res.sources == ("requirements.txt", "reqs/base.txt", "test-requirements.txt")
    assert res.categories.runtime == (
        DependencyRequest("flask", ("==3.1.0",), hashes=(H1,)),
        DependencyRequest("uvicorn", (">=0.30",), extras=("standard",)),
    )
    assert res.categories.test_dev == (DependencyRequest("pytest", (">=8.3",)),)


@pytest.mark.parametrize(
    "line",
    [
        "-i https://evil.invalid/simple",
        "--index-url=https://evil.invalid/simple",
        "--extra-index-url https://evil.invalid/simple",
        "--find-links https://evil.invalid",
        "--trusted-host evil.invalid",
        "-e ../other",
        "-c constraints.txt",
        "flask==3.1.0 --index-url https://evil.invalid/simple",
        "flask==3.1.0 --hash=md5:0123456789abcdef",
        "--hash=sha256:" + H1,
        "-r ../outside.txt",
        "-r /etc/passwd",
        r"-r reqs\base.txt",
        "https://evil.invalid/pkg.whl",
        "git+https://github.com/org/repo.git",
        "./local-pkg",
    ],
)
def test_requirements_rejects_unsafe_lines(tmp_path: Path, line: str) -> None:
    (tmp_path / "requirements.txt").write_text(line + "\n")
    with pytest.raises(DependencyPolicyError, match="dependency_manifest_invalid"):
        discover_repository_dependencies(tmp_path, target_env=LINUX_X86)


def test_requirements_include_cycle_and_depth_limits(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("-r b.txt\n")
    (tmp_path / "b.txt").write_text("-r requirements.txt\n")
    with pytest.raises(DependencyPolicyError, match="dependency_manifest_invalid"):
        discover_repository_dependencies(tmp_path, target_env=LINUX_X86)

    # Exceeding depth limit
    (tmp_path / "requirements.txt").write_text("-r d1.txt\n")
    for idx in range(1, MAX_INCLUDE_DEPTH + 2):
        nxt = f"d{idx + 1}.txt"
        content = f"-r {nxt}\n" if idx <= MAX_INCLUDE_DEPTH else "flask==3.1.0\n"
        (tmp_path / f"d{idx}.txt").write_text(content)
    with pytest.raises(DependencyPolicyError, match="dependency_manifest_invalid"):
        discover_repository_dependencies(tmp_path, target_env=LINUX_X86)


def test_symlink_and_fifo_manifests_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}_secret.txt"
    outside.write_text("flask==3.1.0\n")
    os.symlink(outside, tmp_path / "requirements.txt")
    with pytest.raises(DependencyPolicyError, match="dependency_manifest_invalid"):
        discover_repository_dependencies(tmp_path, target_env=LINUX_X86)

    (tmp_path / "requirements.txt").unlink()
    os.mkfifo(tmp_path / "requirements.txt")
    with pytest.raises(DependencyPolicyError, match="dependency_manifest_invalid"):
        discover_repository_dependencies(tmp_path, target_env=LINUX_X86)


def test_discover_dependencies_from_files_and_merge(tmp_path: Path) -> None:
    files = {
        "pyproject.toml": (
            b'[project]\nname = "svc"\ndependencies = ["fastapi>=0.110"]\n'
            b'[project.optional-dependencies]\ndev = ["pytest>=8.0"]\n'
        )
    }
    discovered = discover_dependencies_from_files(files, max_packages=10, target_env=LINUX_X86)
    merged = merge_dependency_requests(
        discovered, ["fastapi==0.115.0", "httpx>=0.28"], max_packages=10
    )
    assert [r.requirement() for r in merged] == [
        "fastapi==0.115.0,>=0.110",
        "httpx>=0.28",
        "pytest>=8.0",
    ]
    with pytest.raises(DependencyPolicyError, match="dependency_conflict"):
        merge_dependency_requests(discovered, ["fastapi==0.100.0"], max_packages=10)
    with pytest.raises(DependencyPolicyError, match="dependency_limit_exceeded"):
        merge_dependency_requests(discovered, ["httpx>=0.28"], max_packages=2)


def test_in_memory_mapping_reader_limits_utf8_and_missing_include() -> None:
    from ai_code_change_agent.domain.dependencies.manifests import MAX_MANIFEST_BYTES

    with pytest.raises(DependencyPolicyError, match="dependency_manifest_invalid"):
        discover_dependencies_from_files({"requirements.txt": b"\xff\xfe"})
    with pytest.raises(DependencyPolicyError, match="dependency_limit_exceeded"):
        discover_dependencies_from_files({"requirements.txt": b"a" * (MAX_MANIFEST_BYTES + 1)})
    with pytest.raises(DependencyPolicyError, match="dependency_manifest_invalid"):
        discover_dependencies_from_files({"requirements.txt": b"-r missing.txt\n"})


def test_requirements_long_form_include_and_space_separated_hash(tmp_path: Path) -> None:
    (tmp_path / "base.txt").write_text(f"flask==3.1.0 --hash sha256:{H1}\n")
    (tmp_path / "requirements.txt").write_text("--requirement=base.txt\n")
    res = discover_repository_dependencies(tmp_path, target_env=LINUX_X86)
    assert res.provisioned == (DependencyRequest("flask", ("==3.1.0",), hashes=(H1,)),)


def test_uv_lock_abi3_wheel_and_invalid_toml_handling(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text(
        _uv_lock_base(
            wheels=(
                f'[{{ url = "https://files.pythonhosted.org/flask-3.1.0-cp38-abi3-manylinux_2_17_x86_64.whl", hash = "sha256:{H1}" }}]'
            )
        )
    )
    res = discover_repository_dependencies(tmp_path, target_env=LINUX_X86)
    assert res.provisioned == (DependencyRequest("flask", ("==3.1.0",), hashes=(H1,)),)

    (tmp_path / "uv.lock").write_text("not = [valid toml")
    with pytest.raises(DependencyPolicyError, match="dependency_manifest_invalid"):
        discover_repository_dependencies(tmp_path, target_env=LINUX_X86)


# ---- legacy requirements.txt projects -----------------------------------------------------------
def _reqs(res: object, attr: str) -> list[str]:
    return sorted(r.requirement() for r in getattr(res.categories, attr))  # type: ignore[attr-defined]


def test_legacy_requirements_with_includes_constraints_markers_and_self_install() -> None:
    files = {
        "requirements.txt": b"-r requirements/base.txt\n-c constraints.txt\n"
        b"Django>=3.2  # LTS\nsix; python_version < '4'\n--index-url https://pypi.org/simple\n",
        "requirements/base.txt": b"celery==5.3.6\n",
        "constraints.txt": b"django<4.0\nkombu<6\n",
        "requirements-dev.txt": b"-r requirements.txt\n-e .\npytest==7.4.4\n",
        "setup.py": b"raise SystemExit('never executed')\n",
    }
    res = discover_dependencies_from_files(files, expected_index_url="https://pypi.org/simple")
    assert res.source_kind == "requirements.txt"
    assert _reqs(res, "runtime") == ["celery==5.3.6", "django<4.0,>=3.2", "six"]
    assert "pytest==7.4.4" in _reqs(res, "test_dev")
    assert "kombu" not in {r.name for r in res.provisioned}  # a constraint never adds a package


def test_requirements_directory_layout_is_discovered() -> None:
    files = {
        "requirements/base.txt": b"celery==5.3.6\n",
        "requirements/prod.txt": b"-r base.txt\ngunicorn==21.2.0\n",
        "requirements/test.txt": b"-r prod.txt\npytest\n",
    }
    res = discover_dependencies_from_files(files)
    assert _reqs(res, "runtime") == ["celery==5.3.6", "gunicorn==21.2.0"]
    assert "pytest" in _reqs(res, "test_dev")


@pytest.mark.parametrize(
    ("extra", "detail"),
    [
        ("--index-url https://evil.example/simple\n", "index_not_allowed"),
        ("--extra-index-url https://pypi.org/simple\n", "forbidden_pip_flag:--extra-index-url"),
        ("-c ../../etc/passwd\n", "manifest_path_escape"),
        ("-e git+https://github.com/x/y.git#egg=y\n", "forbidden_pip_flag:-e"),
    ],
)
def test_legacy_requirements_refuse_unsafe_options(extra: str, detail: str) -> None:
    files = {"requirements.txt": b"six\n" + extra.encode()}
    with pytest.raises(DependencyPolicyError) as err:
        discover_dependencies_from_files(files, expected_index_url="https://pypi.org/simple")
    assert err.value.detail == detail


def test_a_constraint_incompatible_with_a_requirement_is_a_conflict() -> None:
    files = {"requirements.txt": b"-c c.txt\nDjango<4\n", "c.txt": b"django>=5\n"}
    with pytest.raises(DependencyPolicyError, match="dependency_conflict"):
        discover_dependencies_from_files(files)


def test_setup_py_alone_is_refused_not_executed() -> None:
    with pytest.raises(DependencyPolicyError) as err:
        discover_dependencies_from_files({"setup.py": b"import os; os.system('x')\n"})
    assert err.value.detail == "unsupported_legacy_manifest:setup.py"
