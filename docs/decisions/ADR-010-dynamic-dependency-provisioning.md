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
- **Egress limit of the resolver:** enforced by Docker networking. The resolver runs only on an `--internal` network; its single exit is an allow-list CONNECT proxy (see *Production readiness* below).
- Verified live (2026-11-25): `six==1.17.0` was resolved from PyPI (hash matches PyPI), cached (second call 0.0 s) and imported offline in a `--network none` container; the mount was read-only and outbound network was unavailable.

## Context

Planning showed that Stories need packages known only after the agent investigates the repository (KAN-7: FastAPI, Uvicorn, Pydantic, httpx). Today packages exist only if an operator bakes them into the single execution image. Everything else is BLOCKED.

## Behaviour before this ADR (C2)

The Deep Agent executes only inside its sandbox, which has no network (plus the other hardening listed in C2). The validation container also has no network. **When dependencies cannot be prepared offline, the attempt is BLOCKED and network access is never granted.** `orchestration/steps.py::dependency_preparation` enforces this by blocking any INSTALL command or any command with `network != none`.

## Decision

1. Unchanged: the coding sandbox and the validation container never have network.
2. The plan carries a structured, pinned dependency request. The PLAN approval (bound to the plan hash) approves exactly those packages.
3. After PLAN approval, the worker (trusted control plane) starts a short-lived **resolver container**:
   - no agent code, no workspace, no application credentials, no Docker socket;
   - network egress restricted to an allow-listed package index;
   - it resolves and downloads **binary wheels only** for the approved pins, generating a hash-locked manifest.
4. The worker verifies the lock against the approval and every file hash. It stores an immutable, content-addressed bundle (also the cache) and mounts it **read-only** into the offline sandbox and validation container.
5. Installation happens offline (`pip install --no-index --require-hashes --no-deps`).
6. Missing wheels, hash mismatches, unapproved or changed dependencies all fail closed as BLOCKED. A changed dependency set changes the plan hash and requires fresh approval.

Amended C2 wording (dependencies clause): "Dependencies: repository profiles plus human-approved, hash-locked wheel bundles prepared by a trusted resolver with egress only to allow-listed package indexes, mounted read-only into the offline sandbox. Anything that cannot be prepared that way (sdist or native builds, unapproved packages) is BLOCKED. The sandbox and validation containers are never given network access."

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

## Outcome

B was approved as an amendment to C2 and implemented in the roadmap order of the design document (see *Implemented form* above). A is kept for stacks needing native builds. C is never adopted.

## Interim change already made (within current C2)

`execution-python:0.2.0` adds hash-pinned fastapi, uvicorn, pydantic and httpx at **build time** (operator action, current process), justified by the stored KAN-7 plan. The sandbox remains offline.

## Owner approval (given; see Status)

- [x] Amending C2 as described above.
- [x] The allowed package index list (default: `https://pypi.org/simple` only).
- [x] Egress enforcement for the resolver container (Docker network + egress proxy, or host firewall).
- [x] Limits (direct and transitive package counts, bundle size, preparation timeout).

## Hardening and verified behaviour (2026-11-26)

**Supported input.** Two sources, merged: (1) the repository's own manifests, discovered at planning (see *Production readiness* below), and (2) the `dependencies` list of the agent's plan. Each entry is a PEP 508 name with version specifiers; URLs, file paths, VCS references, pip options and conflicting duplicates are refused (`dependency_request_invalid`, `dependency_manifest_invalid`, `dependency_conflict`). Nothing is ever installed from the network as a fallback.

**Resolution.** One resolver container (`pip download --only-binary=:all:`) runs in the execution image itself, so the Python version (3.12) and platform are the sandbox's. Transitive dependencies are resolved and every wheel is recorded in `requirements.lock` (`name==version --hash=sha256:...`). Only `DEPENDENCY_INDEX_URL` is used (https, no credentials, validated at start-up). Resolution is as deterministic as the index allows: an unpinned request can resolve to a newer release later; that produces a new lock, never a silent change of an existing bundle.

**Integrity.** `lock.json` records the wheel hashes, the lock hash, the execution image's immutable id and a digest of every file in the installed `site/` tree (the directory the sandbox mounts). Every reuse re-verifies all of them. A bundle that fails is moved to `.corrupt-*` (kept for inspection) and rebuilt once; if the rebuild fails, the attempt fails. Validation additionally checks that its bundle has the same lock hash as the `dependency_lock` artifact recorded at implementation (`dependency_bundle_changed` otherwise).

**Cache identity.** The key is sha256 of image tag, image id, index URL and the normalised requests. A different request set, Python version or image build gives a different key. Bundles are immutable and read-only; two repositories that request the same set share one read-only bundle, so nothing mutable is ever shared.

**Publication.** Each build runs in a private `.tmp-*` directory that is frozen and then renamed into place. A concurrent build of the same key keeps the first published bundle; partial builds are deleted.

**Limits.** At most `DEPENDENCY_MAX_PACKAGES` wheels and `DEPENDENCY_MAX_BYTES` downloaded; the installed tree is limited to 4x that; `DEPENDENCY_TIMEOUT_SECONDS` per container; resolver/installer containers: read-only root, all capabilities dropped, no-new-privileges, non-root, 1 GiB memory, 256 pids.

**Failure codes** (become the attempt's `failure_code` and appear in the Jira comment; no PR is created): `dependency_request_invalid`, `dependency_conflict`, `dependency_limit_exceeded`, `dependency_wheel_unavailable`, `dependency_python_unsupported`, `dependency_preparation_timeout`, `dependency_preparation_failed` (index/Docker unavailable), `dependency_install_failed`, `dependency_image_unavailable`, `dependency_bundle_corrupted`, `dependency_bundle_changed`, `dependency_preparation_unavailable`.

**Verified against real Docker and PyPI** (`tests/integration/test_dependency_bundle_docker.py`): `tabulate==0.9.0` (not in the image) is resolved, hash-locked and installed offline. A `--network none`, read-only, non-root sandbox imports exactly 0.9.0, cannot open a socket and cannot write to `/opt/deps`. Without the bundle the import fails (no fetch). A second preparer reuses the cache. Two concurrent preparers publish one bundle. A modified file in `site/` is detected, quarantined and rebuilt. A non-existent version fails with `dependency_wheel_unavailable` and publishes nothing.

**Known limitations.**
- Published-hash verification trusts the index operator's own listing (same origin as the wheel, fetched independently by the worker over TLS). It does not detect a compromised index that rewrites both.
- Wheels only: packages without a wheel for this platform are refused.
- Supply-chain provenance (PEP 740): measured 2026-11-26, about half of common packages publish attestations on PyPI (e.g. requests, pydantic, urllib3, pytest, flask, sqlalchemy do; fastapi, django, celery, boto3, anthropic, langgraph do not). The worker records per wheel whether an attestation is published (`attestation_published` in `lock.json`) but does NOT verify it cryptographically, so a compromised index that serves a matching wheel and hash is not detected. Recommended next control: verify published attestations with `pypi-attestations`/sigstore against the expected Trusted Publisher identity, and keep an allow-list of packages that must be attested.
- `setup.py`, `setup.cfg`, `Pipfile` and `poetry.lock` are not read (reading `setup.py` would mean executing it); a repository with only those fails with `dependency_manifest_invalid`.
- A live Jira-triggered Story that needs a new dependency has not been run (needs approval for a Story, model calls and a draft PR).

## Production readiness (2026-11-26, second pass)

**Repository manifest discovery** (`domain/dependencies/manifests.py`, run at planning, off the event loop, recorded as the `repository_dependencies` artifact). Files are read as data only (never executed), confined to the repository (no symlinks, FIFOs, `..` or absolute paths) and size-limited. Precedence: `uv.lock` (exact pins and hashes) > `pyproject.toml` (`[project]` dependencies, optional groups, `[dependency-groups]`) > requirements files.

Requirements files (older projects): `requirements.txt`, `requirements-dev/test*.txt`, `requirements_dev.txt`, `test-/dev-requirements.txt`, and the `requirements/{base,common,prod,production}.txt` + `requirements/{dev,development,test,tests,testing}.txt` layout. Supported inside them: `-r` includes (depth and count limited), `-c` constraint files (narrow versions, never add packages; an incompatible constraint is `dependency_conflict`), `--hash=sha256:` pins (enforced against the downloaded wheel), environment markers (evaluated for the execution image), `-e .` / `.` (self install, skipped: tests run from the source tree) and `-i/--index-url` equal to the configured index. Refused: any other index, `--extra-index-url`, `--find-links`, `--trusted-host`, other `-e` targets, URLs, VCS and paths. Runtime and test/dev dependencies are recorded separately; both are provisioned because validation runs the tests.

Checked against every branch of the registered repositories: `pyproject.toml` (automate_connector KAN-7, reynolds_connector KAN-8), `requirements.txt` (autoSoft_connector KAN-9), no manifest (all `master` branches, cdk_connector KAN-9, smoke branch).

**Published-hash verification** (`execution/docker/metadata_verifier.py`). Before a bundle is published, the worker fetches the index's project page itself (HTTPS, certificate verified, redirects refused, 8 MiB cap) as PEP 691 JSON or PEP 503 HTML and requires every wheel filename to be listed with the identical sha256. A mismatch fails with `dependency_hash_mismatch`. A missing published hash or an unavailable page fails with `dependency_metadata_unavailable`, unless `DEPENDENCY_ALLOW_UNVERIFIED_INDEX=true`, in which case the wheel is recorded as `unverified`; a mismatch is never excused. Results are stored in `lock.json` (`verification`); a cached bundle without them is quarantined and rebuilt.

**Resolver egress** (`execution/docker/resolver_network.py`, `egress_proxy.py`). The resolver runs on `DEPENDENCY_RESOLVER_NETWORK` (default `aica-dep-resolver`), which must be `--internal`: it is created if missing, and an existing non-internal network is refused. For each preparation a hardened proxy container (read-only, all capabilities dropped, uid 65534, 128 MiB, self-expiring) joins that network and the bridge. It allows only `CONNECT <host>:443` to the index host (plus `files.pythonhosted.org` for PyPI), never IP literals, and refuses names that resolve to non-public addresses.

Measured with real Docker: direct connections to the internet, `host.docker.internal:5432` and `169.254.169.254` are unreachable; through the proxy only the index succeeds, while `example.com`, IP literals, plain HTTP and metadata are refused. The coding sandbox and the installer stay `--network none`.

**Crash recovery** (`tests/integration/test_crash_recovery.py`). A separate worker process is killed with a real SIGKILL at each of these points: before download, mid-download (truncated wheel), during verification, mid-install, after freezing but before the rename, immediately before the rename, immediately after it, and after preparation but before the workflow records it. In every case no partial directory becomes a cache entry, an earlier valid bundle stays intact and is reused, and a fresh worker rebuilds the bundle or, once it is published, reuses it after full verification. Stale `.tmp-*` build directories older than twice the timeout are swept. Four concurrent worker processes publish exactly one bundle.

**Orphaned egress proxies.** Each proxy container is labelled with its owner's host and PID (`aica.owner.host`, `aica.owner.pid`). Before opening a route, a worker removes proxies on its own host whose owner process no longer exists; proxies of live workers and of other hosts are never touched (they still expire via `--lifetime`). Verified with a real SIGKILL: the dead worker's proxy is removed by the next route (`tests/integration/test_resolver_network.py`).

**Workflow-level crash.** A worker SIGKILLed while holding a child job (`tests/integration/test_worker_crash_db.py`, real PostgreSQL): the lease blocks other workers until it expires, a re-enqueue of the same child run is refused by its dedupe key, then exactly one worker reclaims it (attempt 2) and the dead owner can no longer heartbeat or complete it.

**Multi-repository.** Bundles are keyed by image tag, image id (Python version and architecture), index and the exact requests including hashes. Incompatible environments therefore never share a bundle, while identical sets share one read-only, verified bundle. A dependency failure fails only that child: siblings stay completed, the parent reports PARTIALLY_COMPLETED with the code, and a retry re-queues only the failed child (`tests/unit/multirepo/test_multirepo.py`).

New failure code: `dependency_metadata_unavailable`.
