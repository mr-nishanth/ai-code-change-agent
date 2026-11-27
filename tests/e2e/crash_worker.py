"""A pipeline worker that is SIGKILLed in the middle of a job (the e2e restart test).

Run as: python -m tests.e2e.crash_worker <world.json>
It composes the real worker (`worker_app`) with the same fakes as the test (`tests.e2e.world.install`)
and processes one job; its model kills the process on the first call, i.e. inside the implementation
run, with the job leased, the attempt IMPLEMENTING and a sandbox container running. Nothing is
released: no finally block, lease release or message delete runs."""

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult

from ai_code_change_agent.composition import worker_app
from tests.e2e.world import build_world, install, make_settings
from tests.support.models import ScriptedChatModel


class KillOnFirstCall(ScriptedChatModel):
    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        print("model-called", flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
        time.sleep(60)  # never reached
        raise AssertionError("unreachable")


async def main(document: dict[str, Any]) -> None:
    settings = make_settings(document["settings"])
    world = build_world(settings, Path(document["bare"]), KillOnFirstCall(script=[]))
    install(world, setattr)
    async with worker_app(settings) as app:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            job = await app.worker.run_once()
            if job is not None:
                print(f"finished {job.id}", flush=True)  # not expected: the model kills us
                return
    print("no job", flush=True)


if __name__ == "__main__":
    asyncio.run(main(json.loads(Path(sys.argv[1]).read_text())))
