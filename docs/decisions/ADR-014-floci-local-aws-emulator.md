# ADR-014: Floci as a local, test-only AWS emulator

Status: accepted (2026-11-27). The Docker-socket exception below is approved by the S2115 task
constraints; the owner should confirm its wording, because it amends the
[ARCHITECTURE.md](../ARCHITECTURE.md#future-worker-deployment) guidance.

**Extended by [ADR-015](ADR-015-dynamodb-sqs-s3-persistence.md) (2026-11-27):** the application now uses
Floci's DynamoDB, SQS, S3 and (optionally) Bedrock Runtime as its local AWS endpoint in development and
test. The "nothing in the application talks to it" rule below is replaced by ADR-015's local endpoint
guard; the isolation, loopback, dummy-credential and Docker-socket terms stay.

## Context

S2115 asks for local proof of the AWS services a future deployment could use (S3, SQS with a DLQ,
Secrets Manager, CloudWatch, ECR, ECS, RDS, Lambda, API Gateway, IAM, STS) without a real AWS account,
real credentials or any change to how the application runs. The only AWS service in application code
today is Bedrock, confined to `providers/llm/factory.py` (`test_sdk_imports_are_confined`).

## Decision

- **Floci 2.5.0 is a test-only emulator.** It runs in its own Compose file,
  [`infra/docker-compose.yml`](../../infra/docker-compose.yml), as project `acca-floci`, pinned by
  digest. `compose.yaml` is not changed and Floci is never part of the application stack.
- **Nothing in the application talks to it.** No `src` client, no `Settings` field, no endpoint or
  credentials for the API, the worker or the sandboxes. Only the `floci` pytest marker
  (`tests/integration/floci/`, helper `tests/support/floci.py`), the host bootstrap scripts
  (`infra/floci/init/`) and the validate-only Terraform in `infra/terraform/local` use it.
- **Separate project and network.** Floci and every container it spawns (Lambda runtimes, ECS tasks,
  RDS `postgres:16-alpine`, the ECR `registry:2` sidecar) sit on the `acca-floci` network, never on
  `ai-code-change-agent_default` next to `acca-postgres` and `acca-api`. `down -v` on the Floci project
  can never remove `acca_pgdata`.
- **Loopback ports only.** `127.0.0.1:4566` (AWS APIs and the ECR registry) and
  `127.0.0.1:7001-7005` (RDS proxies). The Floci entrypoint listens on `0.0.0.0` inside the container,
  so the loopback host binding is the exposure control.
- **Dummy credentials only.** `test`/`test`, region `us-east-1`. Tests build an explicit boto3
  Session; scripts unset `AWS_PROFILE` and session variables and point `AWS_CONFIG_FILE` and
  `AWS_SHARED_CREDENTIALS_FILE` at `/dev/null`. Scripts, tests and the Terraform endpoint variable
  refuse any non-loopback endpoint, so a real account is unreachable even when a developer has
  credentials configured.
- **Explicit Docker-socket exception.** [ARCHITECTURE.md](../ARCHITECTURE.md#future-worker-deployment)
  says: *"Do not mount the Docker socket into a general-purpose container, and never into a sandbox."*
  Floci needs `/var/run/docker.sock` to run Lambda, ECS, RDS and ECR as sibling containers. It is
  granted to **this one container only**, on these terms:
  - Floci is a single-purpose local emulator, not a general-purpose container, and runs only on a
    developer machine for tests; never in CI against shared daemons, never in staging or production;
  - the socket is never mounted into `acca-api`, the sandboxes, the dependency resolver or any
    container Floci spawns (Floci itself rejects the socket as an ECS host volume);
  - the socket gives Floci root-equivalent control of the host Docker daemon (it can start, stop and
    remove any container, including `acca-postgres` and live sandboxes). Floci's own reconciliation
    only touches containers labelled `floci=true`; heavy Floci tests are run only when no agent run
    or live sandbox is active;
  - stop Floci (`docker compose -f infra/docker-compose.yml down`) when it is not in use.
- **Storage:** `FLOCI_STORAGE_MODE=persistent` in the dedicated volume, with
  `FLOCI_STORAGE_PRUNE_VOLUMES_ON_DELETE=true` so RDS/ECR volumes go with their resource.
- **Only documented Floci 2.5.0 settings**, see the
  [capability matrix](../aws/FLOCI-SERVICE-CAPABILITY-MATRIX.md).

## Consequences

- The default `uv run pytest` run is unchanged: Floci tests skip with a reason when Floci is absent.
- The architecture guards (`tests/unit/test_architecture.py`) are unchanged.
- Moving any component onto an AWS service is a separate, unapproved decision; see
  [AWS-DEPLOYMENT-READINESS.md](../aws/AWS-DEPLOYMENT-READINESS.md).

## Rejected alternatives

- **Overlay on `compose.yaml`** (`-f compose.yaml -f infra/docker-compose.yml`): merges into project
  `ai-code-change-agent` and puts Floci-spawned containers on the application network.
- **LocalStack or several single-service emulators:** more infrastructure for the same proof.
- **Mock mode** (`FLOCI_SERVICES_ECS_MOCK`, `FLOCI_SERVICES_RDS_MOCK`): would avoid the socket but runs
  no containers, so ECS exit codes, RDS connections and Lambda invocations would prove nothing.
- **A `src` AWS client factory and `Settings` fields:** no application code needs them, and they would
  widen the SDK confinement rule.
