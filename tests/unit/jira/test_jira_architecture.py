import ast
import inspect
import tomllib
from pathlib import Path

from ai_code_change_agent.application.jira.ports import JiraIssueReader
from ai_code_change_agent.integrations.jira.client import JiraHttpClient
from ai_code_change_agent.integrations.jira.issues import JiraCloudIssueReader

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src" / "ai_code_change_agent"
WRITE_VERBS = {"post", "put", "patch", "delete"}
WRITE_NAMES = (
    "create",
    "update",
    "add",
    "delete",
    "set",
    "assign",
    "transition",
    "upload",
    "remove",
    "write",
    "edit",
    "post",
    "put",
)


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
    return out


def test_domain_has_no_http_or_web_imports() -> None:
    for f in (SRC / "domain").rglob("*.py"):
        assert not {i.split(".")[0] for i in _imports(f)} & {
            "httpx",
            "fastapi",
            "requests",
            "aiohttp",
        }, f


def test_application_depends_only_inward() -> None:
    banned_roots = {"httpx", "fastapi", "starlette", "sqlalchemy", "requests"}
    banned_prefixes = (
        "ai_code_change_agent.integrations",
        "ai_code_change_agent.persistence",
        "ai_code_change_agent.api",
    )
    for f in (SRC / "application").rglob("*.py"):
        for name in _imports(f):
            assert name.split(".")[0] not in banned_roots, (f, name)
            assert not name.startswith(banned_prefixes), (f, name)


REPORTER = SRC / "integrations" / "jira" / "reporting.py"


def test_no_http_write_calls_in_jira_integration() -> None:
    """Jira is read-only except the single outcome-comment reporter (owner decision C4)."""
    files = list((SRC / "integrations" / "jira").rglob("*.py")) + list(
        (SRC / "application" / "jira").rglob("*.py")
    )
    assert files
    for f in files:
        if f == REPORTER:
            continue
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in WRITE_VERBS:
                raise AssertionError(f"{f}: .{node.attr}")
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.upper() in {"POST", "PUT", "PATCH", "DELETE"}
            ):
                raise AssertionError(f"{f}: {node.value!r}")


def test_the_reporter_only_adds_a_comment() -> None:
    source = REPORTER.read_text()
    posts = [
        n
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Attribute) and n.attr in WRITE_VERBS
    ]
    assert [p.attr for p in posts] == ["post"]
    assert "/rest/api/3/issue/{issue_key}/comment" in source
    for verb in ("transitions", "/assignee", "editmeta", ".put(", ".delete(", ".patch("):
        assert verb not in source


def test_only_get_requests_are_issued_by_the_client() -> None:
    source = (SRC / "integrations" / "jira" / "client.py").read_text()
    assert '"GET"' in source


def test_public_surface_is_read_only() -> None:
    def public(cls: type) -> set[str]:
        return {n for n, _ in inspect.getmembers(cls, inspect.isfunction) if not n.startswith("_")}

    for cls in (JiraIssueReader, JiraCloudIssueReader, JiraHttpClient):
        for name in public(cls):
            assert name.startswith("get_") or name == "aclose", (cls, name)
            assert not name.startswith(WRITE_NAMES), (cls, name)


def test_no_http_route_exposes_jira_rest() -> None:
    for f in (SRC / "api").rglob("*.py"):
        if f.name == "composition.py":
            continue
        imports = _imports(f)
        assert not any(
            i.startswith(
                (
                    "ai_code_change_agent.application.jira",
                    "ai_code_change_agent.integrations.jira.issues",
                    "ai_code_change_agent.integrations.jira.client",
                )
            )
            for i in imports
        ), f


def test_httpx_is_a_runtime_dependency() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert any(d.lower().startswith("httpx") for d in data["project"]["dependencies"])
