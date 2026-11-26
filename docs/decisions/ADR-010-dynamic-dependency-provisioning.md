# ADR-010: Dynamic dependency provisioning by trusted infrastructure

- **Status:** Accepted by the owner and implemented in its simplified form (below).
- **Date:** 2026-11-25
- **Amends:** decision C2 (docs/ARCHITECTURE-DECISIONS.md)
- **Details:** [DYNAMIC-DEPENDENCY-PROVISIONING.md](../DYNAMIC-DEPENDENCY-PROVISIONING.md)

## Implemented form

- **No human dependency approval.** The plan's `dependencies` list (requirement strings) is validated by `domain/dependencies/policy.py`, which refuses URLs, paths, VCS refs, extras, markers, options, duplicates and over-limit counts. The automatic PLAN policy rejects invalid requests.
- `execution/docker/dependencies.py::DockerDependencyPreparer` (port `application/dependencies/ports.py`):
  1. A **resolver container** (bridge network, read-only rootfs, caps dropped, non-root, no app env, no workspace) runs only `pip download --only-binary=:all: --index-url <DEPENDENCY_INDEX_URL>`.
  2. The worker accepts wheels only, enforces `DEPENDENCY_MAX_PACKAGES` and `DEPENDENCY_MAX_BYTES`, and writes `requirements.lock` with sha256 hashes.
  3. An **installer container** (`--network none`) runs `pip install --no-index --require-hashes --no-deps --target`.
  4. The bundle is frozen read-only and renamed atomically to `ARTIFACT_ROOT/dependency-bundles/<key>`. The key is a sha256 of image + index + requests, and also serves as the cache. Every reuse re-hashes every wheel.
- The bundle's `site` directory is mounted **read-only at `/opt/deps`** (`PYTHONPATH`, `PATH`) into the agent sandbox and the validation container. The runtime refuses any directory outside the bundle root. The sandbox and validation containers stay `--network none`.
- The lock is recorded per attempt as a `dependency_lock` artifact.
- **Egress limit of the resolver:** enforced by its fixed argv (single index), not by a firewall. A host egress rule restricting it to the index is a recommended operator hardening.
- Verified live (2026-11-25): `six==1.17.0` was resolved from PyPI (hash matches PyPI), cached (second call 0.0 s) and imported offline in a `--network none` container; the mount was read-only and outbound network was unavailable.

## Context

Planning showed that Stories need packages known only after the agent investigates the repository (KAN-7: FastAPI, Uvicorn, Pydantic, httpx). Today packages exist only if an operator bakes them into the single execution image. Everything else is BLOCKED.

## Current approved behaviour (C2)

The Deep Agent executes only inside its sandbox, which has no network (plus the other hardening listed in C2). The validation container also has no network. **When dependencies cannot be prepared offline, the attempt is BLOCKED and network access is never granted.** `orchestration/steps.py::dependency_preparation` enforces this by blocking any INSTALL command or any command with `network != none`.

## Proposed behaviour

1. Unchanged: the coding sandbox and the validation container never have network.
2. The plan carries a structured, pinned dependency request. The PLAN approval (bound to the plan hash) approves exactly those packages.
3. After PLAN approval, the worker (trusted control plane) starts a short-lived **resolver container**:
   - no agent code, no workspace, no application credentials, no Docker socket;
   - network egress restricted to an allow-listed package index;
   - it resolves and downloads **binary wheels only** for the approved pins, generating a hash-locked manifest.
4. The worker verifies the lock against the approval and every file hash. It stores an immutable, content-addressed bundle (also the cache) and mounts it **read-only** into the offline sandbox and validation container.
5. Installation happens offline (`pip install --no-index --require-hashes --no-deps`).
6. Missing wheels, hash mismatches, unapproved or changed dependencies all fail closed as BLOCKED. A changed dependency set changes the plan hash and requires fresh approval.

Proposed new C2 wording (dependencies clause): "Dependencies: repository profiles plus human-approved, hash-locked wheel bundles prepared by a trusted resolver with egress only to allow-listed package indexes, mounted read-only into the offline sandbox. Anything that cannot be prepared that way (sdist or native builds, unapproved packages) is BLOCKED. The sandbox and validation containers are never given network access."

## Security implications

- **New egress:** one component gains network, the resolver container, limited to package indexes. It never handles Story text, repository content or agent output beyond the validated pin list, so exfiltration through it is limited to the package names and versions themselves.
- **Supply chain:** approved packages run inside the offline sandbox and validation container. A malicious but approved package can harm only an isolated, network-less container, not the host. Typosquats and injected requests are mitigated by human approval of exact pins, deny and allow lists, and normalised names at review.
- **Integrity:** content hashes are recorded at resolution, re-verified before every mount and again by pip. Bundles are immutable.
- **No install-time code execution anywhere** (wheels only, no builds).
- Attempts should pin the execution-image digest and lock hash, closing the current gap that the image is chosen at run time.

## Alternatives

| Option | Verdict |
|---|---|
| A. Prebuilt image per stack (current mechanism) | Safe and simple. Rebuild per new package; one global image grows for every repository. **Kept** as the baseline and for controlled native builds |
| B. Trusted dynamic preparation (this ADR) | Recommended: dynamic, offline sandbox, auditable |
| C. Network-enabled sandbox (allow-list proxy) | **Rejected**: prompt-injected agents could install arbitrary or typosquatted packages, exfiltrate code through allowed hosts (URLs and package-name lookups), reach internal networks and run install scripts. It removes the main isolation boundary |
| D. Agent-requested install at runtime through a broker tool | Rejected: it gives the model an acquisition capability and bypasses the plan-time approval |

## Recommendation

Approve B as an amendment to C2, implemented in the roadmap order of the design document. Keep A for stacks needing native builds. Never adopt C.

## Interim change already made (within current C2)

`execution-python:0.2.0` adds hash-pinned fastapi, uvicorn, pydantic and httpx at **build time** (operator action, current process), justified by the stored KAN-7 plan. The sandbox remains offline.

## Required owner approval

- [ ] Approve amending C2 as proposed.
- [ ] Approve the allowed package index list (default: `https://pypi.org/simple` only).
- [ ] Approve egress enforcement for the resolver container (Docker network + egress proxy, or host firewall).
- [ ] Approve limits (direct and transitive package counts, bundle size, preparation timeout).
