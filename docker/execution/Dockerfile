# Execution image for the "python" toolchain profile: the Deep Agent sandbox and the independent
# validation workspace both run it.
#
# Built once by an operator (network at BUILD time only). At RUN time the application starts it
# with --network none, a read-only root filesystem, all capabilities dropped, no-new-privileges,
# Docker's default seccomp profile, resource limits and a non-root --user. It contains no
# credentials. Repositories whose dependencies are not in this image are reported as BLOCKED
# (dependency preparation needs network) instead of enabling network access.
# The base image is pinned by digest; Python packages are pinned with hashes (--require-hashes).
FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt /tmp/requirements.txt
RUN pip install --require-hashes --no-deps -r /tmp/requirements.txt \
 && rm -f /tmp/requirements.txt \
 && pip check

# python3 (file reads by the Deep Agents sandbox tools), /bin/sh, coreutils timeout, grep and
# find come from the base image. Nothing here needs root at run time.
WORKDIR /workspace
LABEL org.opencontainers.image.title="ai-code-change-agent-execution-python" \
      org.opencontainers.image.description="Network-less execution sandbox (python toolchain: pytest, ruff, mypy; fastapi, uvicorn, pydantic, httpx)"
