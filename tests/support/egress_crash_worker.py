"""Opens a real resolver egress route (proxy container running), prints its name, then is
SIGKILLed so the route's cleanup never runs. Usage: python -m tests.support.egress_crash_worker
<image> <network>"""

import asyncio
import os
import signal
import sys
import time

from ai_code_change_agent.execution.docker.process import SubprocessRunner
from ai_code_change_agent.execution.docker.resolver_network import ResolverEgress


async def main(image: str, network: str) -> None:
    egress = ResolverEgress(
        runner=SubprocessRunner(), image=image, allow_hosts=frozenset({"pypi.org"}),
        network=network,
    )  # fmt: skip
    async with egress.route() as route:
        print(route.proxy_url.split("//")[1].split(":")[0], flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
        time.sleep(60)  # noqa: ASYNC251 - block: never run past the crash point


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
