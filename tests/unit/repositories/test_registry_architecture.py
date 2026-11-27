import ast
import re
from pathlib import Path

from ai_code_change_agent.application.git_providers.ports import GitProviderClient
from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.main import create_app

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src" / "ai_code_change_agent"


def imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
    return out


def test_domain_and_application_are_framework_and_orm_free() -> None:
    banned = {
        "sqlalchemy",
        "fastapi",
        "starlette",
        "httpx",
        "psycopg",
        "alembic",
        "boto3",
        "langchain",
        "langgraph",
    }
    for layer in ("domain", "application"):
        for f in (SRC / layer).rglob("*.py"):
            assert not {i.split(".")[0] for i in imports(f)} & banned, f


def test_application_never_imports_adapters_or_cli() -> None:
    for f in (SRC / "application").rglob("*.py"):
        for name in imports(f):
            assert not name.startswith(
                (
                    "ai_code_change_agent.persistence",
                    "ai_code_change_agent.integrations",
                    "ai_code_change_agent.api",
                    "ai_code_change_agent.cli",
                )
            ), (f, name)


def test_no_git_host_sdks_and_no_provider_names_in_shared_code() -> None:
    sdk = re.compile(r"^(github|gitlab|bitbucket|pygithub|dulwich|git|pygit2)(\.|$)", re.IGNORECASE)
    for f in SRC.rglob("*.py"):
        assert not [i for i in imports(f) if sdk.match(i)], f
    for layer in ("domain", "application"):
        for f in (SRC / layer).rglob("*.py"):
            for node in ast.walk(ast.parse(f.read_text())):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    assert not re.search(
                        r"\b(github|gitlab|bitbucket)\b", node.value, re.IGNORECASE
                    ), (
                        f,
                        node.value,
                    )


def test_git_provider_contract_is_minimal() -> None:
    members = {m for m in dir(GitProviderClient) if not m.startswith("_")}
    assert members == {"describe_repository"}
    banned = ("clone", "checkout", "branch", "commit", "push", "pull", "merge", "fetch")
    assert not [m for m in members if any(b in m for b in banned)]


def test_no_process_execution_or_shell_in_product_code() -> None:
    for f in SRC.rglob("*.py"):
        if "migrations" in f.parts:
            continue
        names = imports(f)
        if f.parent.name == "docker" and f.parent.parent.name == "execution":
            continue  # the controlled execution boundary: fixed docker argv only (own tests)
        assert not names & {"subprocess", "pty", "multiprocessing"}, f
        text = f.read_text()
        assert "os.system" not in text and "shell=True" not in text and "os.popen" not in text, f


def test_no_registry_or_jira_admin_http_routes_exist() -> None:
    app = create_app(Settings.model_validate({"app_env": "test"}))
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not [
        p for p in paths if re.search(r"repositor|registry|alias|profile|mapping|internal|admin", p)
    ]
    for f in (SRC / "api").rglob("*.py"):
        assert not [
            i for i in imports(f) if "application.repositories" in i and f.name != "composition.py"
        ], f


def test_no_vendor_centric_names_in_registry_code() -> None:
    pattern = re.compile(r"vendor", re.IGNORECASE)
    for sub in (
        "domain/repositories",
        "application/repositories",
        "application/git_providers",
        "persistence/dynamodb/registry_store.py",
        "cli",
    ):
        target = SRC / sub
        files = [target] if target.is_file() else list(target.rglob("*.py"))
        for f in files:
            assert not pattern.search(f.read_text()), f


def test_secrets_are_not_modelled_as_item_attributes() -> None:
    """No attribute the DynamoDB registry store writes or reads is named like a secret."""
    banned = re.compile(r"token|password|secret|credential|private_key|api_key", re.IGNORECASE)
    tree = ast.parse((SRC / "persistence" / "dynamodb" / "registry_store.py").read_text())
    names = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "access_reference" in names  # the store does write attribute names as literals
    assert not {n for n in names if banned.search(n)}
