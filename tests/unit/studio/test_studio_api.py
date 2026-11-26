"""Deep Agent studio (dev-only Agent Server routes): path confinement and pricing."""

from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from studio.api import app
from studio.api_files import safe_path as _safe
from studio.workspaces import SeedBaseline


def test_paths_are_confined_to_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / "work"
    (root / "app").mkdir(parents=True)
    (root / "app" / "x.py").write_text("x")
    assert _safe(root, "app/x.py") == root / "app" / "x.py"
    assert _safe(root, "/workspace/app/x.py") == root / "app" / "x.py"
    (tmp_path / "secret").write_text("s")
    (root / "link").symlink_to(tmp_path / "secret")
    for bad in ("../secret", "app/../../secret", "link"):
        with pytest.raises(HTTPException):
            _safe(root, bad)


async def test_seed_drops_fixture_suffix_and_skips_caches(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py.fixture").write_text("a")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "c.pyc").write_bytes(b"c")
    names = [f.path async for f in SeedBaseline(tmp_path).files()]
    assert names == ["app/a.py"]


def test_cost_route_prices_provider_usage_with_the_project_table() -> None:
    client = TestClient(app)
    res = client.post(
        "/studio/cost",
        json={"calls": [{"model": "claude-haiku-5-5", "input_tokens": 100000}, {"model": "x"}]},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["total_usd"] == "0.010000" and body["unpriced_calls"] == 1
    assert client.get("/sandbox/unknown-thread/tree").status_code == 404


def test_thread_history_prices_provider_usage_without_double_counting_cache() -> None:
    from studio.history import summarize_thread

    ai = {
        "type": "ai",
        "content": "done",
        "response_metadata": {"model": "claude-haiku-5-5"},
        "usage_metadata": {
            "input_tokens": 100_000,  # includes 60k cache reads + 20k 5m cache writes
            "output_tokens": 1_000,
            "input_token_details": {
                "cache_read": 60_000,
                "ephemeral_5m_input_tokens": 20_000,
                "ephemeral_1h_input_tokens": 0,
            },
        },
    }
    thread = {
        "thread_id": "t1",
        "metadata": {"graph_id": "coding_agent"},
        "status": "idle",
        "values": {
            "messages": [{"type": "human", "content": "do it"}, ai],
            "structured_response": {"outcome": "implemented"},
            "todos": [
                {"content": "a", "status": "completed"},
                {"content": "b", "status": "pending"},
            ],
        },
    }
    row = summarize_thread(thread)
    # per MTok: 20k x 0.10 + 1k x 0.50 + 20k x 0.125 + 60k x 0.01 = 0.002+0.0005+0.0025+0.0006
    assert row["cost_usd"] == "0.005600"
    assert (row["title"], row["outcome"], row["calls"]) == ("do it", "implemented", 1)
    assert (row["todos_done"], row["todos_total"], row["tokens_in"]) == (1, 2, 100_000)
    planner = summarize_thread({**thread, "values": {"structured_response": {"summary": "x"}}})
    assert planner["outcome"] == "plan" and planner["calls"] == 0


def test_worker_thread_ids_map_to_pipeline_stages() -> None:
    from studio.live import _THREAD, _phase

    a = "f72089de-ea66-45f4-a3b0-3055432e2719"
    assert _phase(f"attempt:{a}:planning:6cfe6af9")["stage"] == "planning"
    assert _phase(f"attempt:{a}:implementation:0:7c72c6f2")["stage"] == "implementation"
    assert _phase(f"attempt:{a}:implementation:1:223c96b3")["stage"] == "correction"
    assert _THREAD.fullmatch(f"attempt:{a}:planning:6cfe6af9")
    assert not _THREAD.fullmatch(f"attempt:{a}")  # the parent workflow thread is not an agent run
