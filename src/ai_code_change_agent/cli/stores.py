"""The DynamoDB, SQS and S3 adapters the operator CLI uses, built from Settings via `AwsClients`.

Everything goes through the application's ports: approvals and attempts through `CodeChangeService`,
the registry through its unit of work, costs through `CostQuery`, multi-repository retries through
`Coordinator`. The CLI is a worker-side producer: a resume (or child) job is written to the table
with its outbox entry and sent to SQS directly; when that send fails the worker's relay sends it.
No SQL database is involved."""

import json

from ai_code_change_agent.application.artifacts.service import ArtifactService
from ai_code_change_agent.application.changes.service import CodeChangeService
from ai_code_change_agent.application.git_providers.ports import GitProviderRegistry
from ai_code_change_agent.application.jira.reports import DurableJiraReporter
from ai_code_change_agent.application.multirepo.coordinator import Coordinator
from ai_code_change_agent.application.repositories.resolver import RepositoryResolver
from ai_code_change_agent.application.repositories.service import RepositoryRegistryService
from ai_code_change_agent.composition import build_coordinator, build_jira_reporter
from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.execution.s3_artifacts import S3ArtifactStore
from ai_code_change_agent.integrations.aws import AwsClients
from ai_code_change_agent.integrations.github.references import GitHubReferenceParser
from ai_code_change_agent.persistence.dynamodb import Table
from ai_code_change_agent.persistence.dynamodb.change_store import DynamoChangeUnitOfWork
from ai_code_change_agent.persistence.dynamodb.jira_report_store import DynamoJiraReportStore
from ai_code_change_agent.persistence.dynamodb.job_queue import DynamoSqsJobQueue
from ai_code_change_agent.persistence.dynamodb.multirepo_store import DynamoMultiRepoStore
from ai_code_change_agent.persistence.dynamodb.registry_store import DynamoRegistryUnitOfWork
from ai_code_change_agent.persistence.dynamodb.usage_store import DynamoUsageStore


class CliStores:
    """One process's adapters over the application table, the job queue and the artifact bucket."""

    def __init__(self, settings: Settings, aws: AwsClients | None = None) -> None:
        self.settings = settings
        self.aws = aws or AwsClients(settings)
        self.table = Table(self.aws.dynamodb, settings.dynamodb_table_name)
        self.changes = CodeChangeService(
            lambda: DynamoChangeUnitOfWork(self.table), max_repairs=settings.max_repair_attempts
        )
        self.usage = DynamoUsageStore(self.table)

    def registry_uow(self) -> DynamoRegistryUnitOfWork:
        return DynamoRegistryUnitOfWork(self.table)

    def registry_service(
        self, providers: GitProviderRegistry | None = None
    ) -> RepositoryRegistryService:
        return RepositoryRegistryService(self.registry_uow, providers=providers)

    def resolver(self) -> RepositoryResolver:
        return RepositoryResolver(self.registry_uow, parsers=(GitHubReferenceParser(),))

    def job_queue(self) -> DynamoSqsJobQueue:
        return DynamoSqsJobQueue(self.table, self.aws.sqs, self.aws.queue_url)

    def reports(self) -> DynamoJiraReportStore:
        return DynamoJiraReportStore(self.table)

    def dlq_url(self) -> str:
        """The job queue's DLQ, from its redrive policy (infra/floci/init/10-sqs.sh)."""
        attrs = self.aws.sqs.get_queue_attributes(
            QueueUrl=self.aws.queue_url(), AttributeNames=["RedrivePolicy"]
        )["Attributes"]
        arn = str(json.loads(attrs["RedrivePolicy"])["deadLetterTargetArn"])
        return str(self.aws.sqs.get_queue_url(QueueName=arn.rsplit(":", 1)[-1])["QueueUrl"])

    def artifacts(self) -> ArtifactService:
        s = self.settings
        return ArtifactService(
            S3ArtifactStore(self.aws.s3, s.s3_bucket_name, max_artifact_bytes=s.artifact_max_bytes),
            max_artifact_bytes=s.artifact_max_bytes,
            max_attempt_bytes=s.artifact_max_attempt_bytes,
        )

    def coordinator(self) -> Coordinator:
        return build_coordinator(
            self.settings,
            store=DynamoMultiRepoStore(self.table),
            queue=self.job_queue(),
            usage=self.usage,
            changes=self.changes,
            artifacts=self.artifacts(),
            resolver=self.resolver(),
            reporter=DurableJiraReporter(build_jira_reporter(self.settings), self.reports()),
        )
