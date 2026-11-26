"""Running the docker CLI: argv lists only, no shell, bounded output, process-group kill."""

import asyncio
import contextlib
import os
import signal
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

_ENV_PASSTHROUGH = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    truncated: bool
    timed_out: bool


class ProcessRunner(Protocol):
    async def run(
        self, argv: Sequence[str], *, deadline: float, max_output: int
    ) -> ProcessResult: ...


async def _drain(stream: asyncio.StreamReader, cap: int) -> tuple[bytes, bool]:
    kept = bytearray()
    truncated = False
    while chunk := await stream.read(65_536):
        room = cap - len(kept)
        if room > 0:
            kept += chunk[:room]
        if len(chunk) > room:
            truncated = True  # keep reading so the child never blocks on a full pipe
    return bytes(kept), truncated


class SubprocessRunner:
    """Starts the docker client with a minimal environment: application secrets are not passed."""

    async def run(self, argv: Sequence[str], *, deadline: float, max_output: int) -> ProcessResult:
        env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        assert proc.stdout is not None and proc.stderr is not None  # noqa: S101 - PIPE requested
        out_task = asyncio.create_task(_drain(proc.stdout, max_output))
        err_task = asyncio.create_task(_drain(proc.stderr, max_output))
        try:
            try:
                await asyncio.wait_for(proc.wait(), deadline)
            except TimeoutError:
                self._kill(proc)
                await proc.wait()
                out, t1 = await out_task
                err, t2 = await err_task
                return ProcessResult(None, out, err, t1 or t2, True)
            out, t1 = await out_task
            err, t2 = await err_task
            return ProcessResult(proc.returncode, out, err, t1 or t2, False)
        except BaseException:  # includes cancellation: never leave the client running
            self._kill(proc)
            for task in (out_task, err_task):
                task.cancel()
            raise

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
