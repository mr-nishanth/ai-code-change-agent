# Execution image for the "node" toolchain profile: the Deep Agent sandbox and the independent
# validation workspace both run it (WORKSPACE_IMAGES=node=ai-code-change-agent/execution-node:0.1.0).
#
# Same run-time hardening as the python image: --network none, read-only root filesystem, all
# capabilities dropped, no-new-privileges, default seccomp, resource limits, non-root --user, no
# credentials. Node.js 22 LTS with npm (bundled) and corepack (pnpm/yarn shims, not activated:
# corepack would download them, which the offline sandbox cannot do).
# python3 is required by the Deep Agents sandbox file tools (read/write/edit/ls/grep).
# Repositories whose tests need node_modules require a dependency bundle; the node bundle
# preparer is not implemented yet, so such repositories fail validation instead of getting network.
FROM node:22-bookworm-slim@sha256:be979e5523c76fa4d5ca4ebd3099078f220aa47ec39c48ad5b5df9314debd6eb

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-minimal \
 && rm -rf /var/lib/apt/lists/* \
 && npm config set update-notifier false --location=global

ENV NODE_ENV=test \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_CACHE=/tmp/.npm

WORKDIR /workspace
LABEL org.opencontainers.image.title="ai-code-change-agent-execution-node" \
      org.opencontainers.image.description="Network-less execution sandbox (node toolchain: node 22, npm)"
