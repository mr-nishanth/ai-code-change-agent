# Floci service capability matrix (S2115)

Status: **verified on 2026-11-27** (S2115 Task 8, clean `down -v` / `up` start, Floci 2.5.0 on Docker
Engine 29.9.2 linux/arm64). A row is `VERIFIED LOCALLY` only for the behaviour its listed test asserts;
everything else the row documents stays as the documentation says. Evidence:
[../testing/S2115-FLOCI-INTEGRATION-REPORT.md](../testing/S2115-FLOCI-INTEGRATION-REPORT.md).

Floci version: **2.5.0** (release 2026-11-15). Image: `floci/floci:2.5.0` (standard variant,
multi-arch `linux/amd64` + `linux/arm64`; also on `public.ecr.aws/floci/floci`). Pinned in `infra/docker-compose.yml` as
`floci/floci:2.5.0@sha256:a0fbf944d7f338cc0856f1ef83831ea749ff2be71956eb7a7fce08fc21458e19`. Sources are files in `github.com/floci-io/floci` at tag `2.5.0`.

Status legend: `SUPPORTED, NOT TESTED` (documented, not yet run here), `PARTIAL EMULATION` (documented
with material gaps versus AWS), `NOT SUPPORTED` (documented as absent), `VERIFIED LOCALLY` (a passing `floci` test on this host proves
the behaviour in the Verified by column; untested features in the same row remain documentation-only).

## Runtime facts

| Item | Value | Source |
|---|---|---|
| Image / tags | `floci/floci:x.y.z` (standard), `x.y.z-compat` (+ Python 3.12, AWS CLI, boto3), `x.y.z-baseline` (ARMv8.0, arm64 only) | `docs/configuration/docker-images.md` |
| arm64 | Standard and compat are multi-arch amd64/arm64 | `docs/configuration/docker-images.md` (Multi-Architecture) |
| Signatures | Release images cosign-signed (keyless, GitHub OIDC), SLSA provenance + SPDX SBOM | `docs/configuration/docker-images.md` |
| API port | `4566` (all AWS APIs and the ECR Docker Distribution endpoint) | `docs/configuration/ports.md` |
| RDS proxy ports | `7001-7099`, must be published; `FLOCI_SERVICES_RDS_PROXY_BASE_PORT` / `_MAX_PORT` | `docs/configuration/ports.md`, `docs/services/rds.md` |
| Lambda Runtime API ports | `12000-12499`, Docker-network only, never published | `docs/configuration/ports.md` |
| ECR backing registry | `registry:2` sidecar on loopback `5100-5199`, not published | `docs/configuration/ports.md`, `docs/services/ecr.md` |
| Health | `GET /_floci/health`; readiness `GET /_floci/init` (`"ready":true`); image HEALTHCHECK runs `/usr/local/bin/healthcheck.sh` (bash, no curl in image) | `docker/healthcheck.sh`, `docker/Dockerfile.native`, `Makefile` (native-up) |
| Storage modes | `FLOCI_STORAGE_MODE` = `memory` (image default) \| `persistent` \| `hybrid` \| `wal`; `FLOCI_STORAGE_PERSISTENT_PATH` (image sets `/app/data`); `FLOCI_STORAGE_PRUNE_VOLUMES_ON_DELETE`; per-service `FLOCI_STORAGE_SERVICES_<SVC>_MODE` | `docs/configuration/storage.md` |
| Container volumes | Named volumes `floci-aws-rds-<id>`, `floci-aws-ecr-registry-data`, labelled `floci=true`; removed on delete in `memory` mode or with prune flag | `docs/configuration/storage.md` |
| Docker socket | Required for Lambda, RDS, ECS, ECR (and ElastiCache, OpenSearch, MSK); `FLOCI_DOCKER_DOCKER_HOST` default `unix:///var/run/docker.sock`; entrypoint adds the socket's group to uid 1001; `FLOCI_RUN_AS_ROOT=true` only if the socket is root-only | `docs/configuration/docker.md`, `docker/entrypoint.sh` |
| Network for spawned containers | Auto-detects Floci's own network; override `FLOCI_SERVICES_DOCKER_NETWORK` (per service `FLOCI_SERVICES_{LAMBDA,RDS,ECS,ECR}_DOCKER_NETWORK`) | `docs/configuration/docker.md`, `docs/configuration/environment-variables.md` |
| Hostname in URLs | `FLOCI_HOSTNAME` (host part of `FLOCI_BASE_URL`, default `http://localhost:4566`) | `docs/configuration/environment-variables.md` |
| Region / account | `FLOCI_DEFAULT_REGION` `us-east-1`, `FLOCI_DEFAULT_ACCOUNT_ID` `000000000000` | `docs/configuration/docker-compose.md` |
| Credentials | Any non-empty; use `test`/`test`. SigV4 check off by default (`FLOCI_AUTH_VALIDATE_SIGNATURES=false`) | `docs/getting-started/aws-setup.md`, `docs/configuration/docker-compose.md` |
| Network exposure | Refuses non-loopback listen unless `FLOCI_SECURITY_ALLOW_UNSAFE_NETWORK_EXPOSURE=true`; the container entrypoint sets it and binds `0.0.0.0` | `docs/configuration/environment-variables.md`, `docker/entrypoint.sh` |
| Init hooks | `/etc/floci/init/{boot,start,ready,stop}.d`, `.sh`/`.py`, lexicographic, fail-fast (start/ready failure stops Floci); aws/boto3 inside only in the compat image | `docs/configuration/initialization-hooks.md` |

## Target services

| Service | Status | What the docs say | Limits / caveats | Source | Verified by (`tests/integration/floci/`) |
|---|---|---|---|---|---|
| S3 | VERIFIED LOCALLY | Buckets, objects, versioning, pre-signed URLs, range reads, bucket policies; path-style always works | S3 Metadata, ABAC, `CreateSession` answer `501`; virtual-hosted style needs DNS | `docs/services/s3.md` (Not Implemented, Addressing Styles) | `test_floci_core.py::test_s3_object_lifecycle_and_presigned_get`, `test_floci_smoke.py::test_s3_object_round_trip` (put/head/get/list/delete, pre-signed GET, path style). Versioning and bucket policies not exercised |
| SQS | VERIFIED LOCALLY | Standard and FIFO queues, long polling, attributes | `QueueUrl` host follows `FLOCI_HOSTNAME`/`FLOCI_BASE_URL` | `docs/services/sqs.md` | `test_floci_core.py::test_sqs_dead_letter_redrive_and_move_back` (standard queues, send/receive, attributes); FIFO not exercised; `QueueUrl` usable from the host with `FLOCI_HOSTNAME` unset |
| SQS DLQ redrive | VERIFIED LOCALLY | `RedrivePolicy` (`deadLetterTargetArn`, `maxReceiveCount`), `StartMessageMoveTask`, `ListMessageMoveTasks` | Redrive moves do not count in `NumberOfMessagesSent` | `docs/services/sqs.md` (Supported Actions, Examples) | `test_floci_core.py::test_sqs_dead_letter_redrive_and_move_back` (`maxReceiveCount` move to the DLQ, `StartMessageMoveTask` back to the source) |
| Secrets Manager | VERIFIED LOCALLY | Create/get/update/list/delete, versions and stages, `BatchGetSecretValue`, random password, rotation sweep, replication | Rotation needs a Lambda; IAM checks only under enforcement | `docs/services/secrets-manager.md` | `test_floci_core.py::test_secrets_manager_versions` (create, get, `PutSecretValue` new version with `AWSCURRENT`, describe, delete). Rotation and replication not exercised |
| CloudWatch Logs | VERIFIED LOCALLY | Groups, streams, `PutLogEvents`, `GetLogEvents`, `FilterLogEvents`, retention, Logs Insights, subscription and metric filters | Metric-filter samples are published off the request thread (read-after-write can lag); `FLOCI_SERVICES_CLOUDWATCHLOGS_MAX_EVENTS_PER_QUERY` 10000 | `docs/services/cloudwatch.md` (CloudWatch Logs) | `test_floci_core.py::test_cloudwatch_logs_put_get_and_filter` (group, stream, `PutLogEvents`, `GetLogEvents`, `FilterLogEvents`); retention set by `infra/floci/init/50-logs.sh` |
| CloudWatch Metrics | VERIFIED LOCALLY | `PutMetricData`, `GetMetricStatistics`, `GetMetricData`, `ListMetrics`, alarms, composite alarms | Alarm actions limited to documented targets (SNS) | `docs/services/cloudwatch.md` (CloudWatch Metrics) | `test_floci_core.py::test_cloudwatch_metrics_put_query_and_list` (`PutMetricData`, `GetMetricStatistics`, `GetMetricData`, `ListMetrics`; reads polled up to 20 s). Alarms not exercised |
| ECR | VERIFIED LOCALLY | Real `registry:2` backing; `docker login`/push/pull through `:4566`; `PutImage`/`BatchGetImage`; tag immutability; digests | On Docker Engine 29+ with the containerd image store (this host) `*.localhost` is treated as HTTPS: use `FLOCI_SERVICES_ECR_URI_STYLE=path` (`localhost:4566/<account>/<region>/<repo>`) | `docs/services/ecr.md` (Emulation Behavior, Configuration) | `test_floci_containers.py::test_ecr_push_and_pull_by_digest` (`docker login`, push and pull by digest through `localhost:4566` path style, `DescribeImages`, `BatchGetImage`). The `floci-aws-ecr-registry` sidecar and its volume outlive `docker compose down -v` |
| ECS | VERIFIED LOCALLY | Clusters, task definitions, `RunTask`/`StopTask`/`DescribeTasks`, services; real Docker containers; `containers[].exitCode`, `stoppedReason`, `imageDigest` | Needs the socket and a user-defined network (`FLOCI_SERVICES_ECS_DOCKER_NETWORK`); `FLOCI_SERVICES_ECS_MOCK=true` runs nothing | `docs/services/ecs.md` (Configuration, Docker socket requirement, Mock mode) | `test_floci_containers.py::test_ecs_task_exit_code_and_stop` (`RunTask` with `launchType=EC2`, `containers[].exitCode` = 7, `StopTask` with `stoppedReason`, task containers removed). Services not exercised |
| ECS awsvpc / Fargate isolation | PARTIAL EMULATION | Tasks run as local containers; awsvpc ports stay private unless `FLOCI_SERVICES_ECS_PUBLISH_AWSVPC_PORTS_TO_HOST=true` | Security groups only with `FLOCI_NETWORK_SECURITY_GROUP_ENFORCEMENT_ENABLED=true`; task role credentials off by default (`FLOCI_SERVICES_ECS_TASK_ROLE_CREDENTIALS_ENABLED`); no Fargate microVM isolation | `docs/services/ecs.md` (Host access, Task IAM role credentials) | Not tested (no Fargate/awsvpc test by design) |
| RDS (PostgreSQL) | VERIFIED LOCALLY | `CreateDBInstance --engine postgres` starts `postgres:16-alpine` behind a Floci TCP auth proxy; endpoint port from the proxy range; master user/password checked at the proxy; IAM DB auth supported | Requested `Port` honoured only inside the proxy range; `FLOCI_SERVICES_RDS_ENDPOINT_HOST` decides the advertised host; `FLOCI_SERVICES_RDS_MOCK=true` = metadata only | `docs/services/rds.md` (Configuration, Supported Engines, Authentication), `docs/configuration/ports.md` | `test_floci_containers.py::test_rds_postgres_create_connect_query_delete` (create, endpoint port in 7001-7005, wire-protocol check through the proxy, then `psql` inside the instance container (no PostgreSQL driver), data volume created and pruned on delete). IAM DB auth not exercised |
| Lambda | VERIFIED LOCALLY | Zip (`ZipFile`/S3) and image functions on official AWS Lambda base images (e.g. `python3.12`), sync/async invoke, event source mappings, async retries and DLQ, destinations | Host architecture by default (`FLOCI_SERVICES_LAMBDA_HONOUR_ARCHITECTURES`); default timeout 3 s; needs the socket | `docs/services/lambda.md` (Configuration, Docker socket requirement) | `test_floci_containers.py::test_lambda_invoke_and_api_gateway_http_and_rest` (zip `python3.12` function, synchronous `Invoke`). Async invoke, event source mappings and destinations not exercised |
| Lambda `InvokeWithResponseStream`, layer permissions | NOT SUPPORTED | No handler | Code signing is not enforced | `docs/services/lambda.md` (Not Implemented) | Not tested (documented as absent) |
| API Gateway REST (v1) | VERIFIED LOCALLY | `AWS_PROXY`, `AWS`, `HTTP`, `HTTP_PROXY`, `MOCK`; invoke at `/restapis/{id}/{stage}/_user_request_/{path}` or `/execute-api/{id}/{stage}/{path}` | `TestInvokeAuthorizer`, model templates, documentation versions, client certificates not implemented | `docs/services/api-gateway.md` (v1, Not Implemented) | `test_floci_containers.py::test_lambda_invoke_and_api_gateway_http_and_rest` (`AWS_PROXY` Lambda integration, `CreateDeployment` stage `dev`, invoke at `/restapis/{id}/dev/_user_request_/hello`) |
| API Gateway HTTP (v2) | VERIFIED LOCALLY | HTTP APIs, Lambda proxy integrations, routes, stages incl. `$default`, authorizers; invoke at `{apiId}.execute-api.localhost.floci.io:4566/{stage}/{path}` (also on `localhost` and `FLOCI_HOSTNAME`) | Host-based routing: send to `127.0.0.1:4566` with a `Host` header to avoid DNS | `docs/services/api-gateway.md` (v2, HTTP API data-plane) | `test_floci_containers.py::test_lambda_invoke_and_api_gateway_http_and_rest` (`AWS_PROXY` route `GET /hello`, `$default` stage, invoke at `127.0.0.1:4566` with `Host: {apiId}.execute-api.localhost.floci.io:4566`) |
| IAM | PARTIAL EMULATION (default config) | Users, roles, policies, access keys, managed policies, simulation | Policies are **not enforced** unless `FLOCI_SERVICES_IAM_ENFORCEMENT_ENABLED=true` | `docs/services/iam.md` (IAM Enforcement Mode), `docs/configuration/environment-variables.md` | `test_floci_core.py::test_sts_identity_and_iam_role_with_inline_policy` proves the API surface only (role, trust policy, inline policy); enforcement stays off, so the status is unchanged |
| STS | VERIFIED LOCALLY | `GetCallerIdentity`, `AssumeRole`, `GetSessionToken`, web identity | Session tags and source identity not modelled | `docs/services/sts.md` (Known limitations) | `test_floci_core.py::test_sts_identity_and_iam_role_with_inline_policy`, `test_floci_smoke.py::test_sts_accepts_dummy_credentials` (`GetCallerIdentity`, account `000000000000`). `AssumeRole` and web identity not exercised |

### Observed on this host (2026-11-27)

- No target row had to be downgraded: every assertion in the `floci` suite passed on a clean start.
- `floci-aws-ecr-registry` (Floci's `registry:2` sidecar) and the `floci-aws-ecr-registry-data` volume are
  created by Floci, not by Compose, so `docker compose -f infra/docker-compose.yml down -v` leaves both,
  and the `acca-floci` network stays "in use" by the sidecar. Remove them explicitly when a fully clean
  slate is needed (`docker rm -f floci-aws-ecr-registry && docker volume rm floci-aws-ecr-registry-data`).

## Adjacent services (not S2115 targets)

| Service | Status | Note | Source |
|---|---|---|---|
| Bedrock Runtime | PARTIAL EMULATION | `stub` backend returns a fixed string; `proxy` backend forwards Converse to an OpenAI-compatible URL; `InvokeModel` on proxy still stubbed; `InvokeModelWithResponseStream` 501. Not used: no real model calls in S2115 | `docs/services/bedrock-runtime.md` (Configuration, Out of Scope) |
| Terraform (`hashicorp/aws` ~> 6.0) | SUPPORTED, NOT TESTED | Per-service `endpoints {}` block, `skip_*` flags, `s3_use_path_style`; S3 backend supported. Here only `terraform -chdir=infra/terraform/local validate` was run (Terraform 1.17.2, passed); no `plan`/`apply` against Floci | `docs/getting-started/terraform.md` |
