"""Typed settings: the environment variable contract of the AI Code Change Agent.

Every variable is documented in .env.example and docs/CONFIGURATION.md. Secrets are SecretStr and
never logged; file credentials (the GitHub App private key) are referenced by absolute path only.
Production-like environments fail fast when required configuration is missing.
"""

import re
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["development", "test", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
_PROJECT_KEY = re.compile(r"[A-Z][A-Z0-9_]{1,31}")
_SCHEMA = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_IMAGE = re.compile(r"[a-z0-9][a-z0-9._/-]{0,199}(:[A-Za-z0-9._-]{1,128})?(@sha256:[0-9a-f]{64})?")
_EMPTY_IS_UNSET = (
    "database_url",
    "jira_base_url",
    "jira_email",
    "jira_api_token",
    "jira_webhook_secret",
    "jira_repository_field",
    "github_app_id",
    "github_app_installation_id",
    "github_app_private_key_path",
    "llm_model",
    "anthropic_api_key",
    "bedrock_region",
    "langsmith_api_key",
    "langsmith_endpoint",
    "langsmith_workspace_id",
    "langsmith_agent_id",
    "langsmith_project",
    "bedrock_credentials_profile",
    "workspace_root",
    "artifact_root",
    "workspace_image",
)


def _split_set(value: object) -> object:
    if isinstance(value, str):
        return frozenset(v.strip() for v in value.split(",") if v.strip())
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ---- application ----------------------------------------------------------------------
    app_env: Environment = "development"
    app_name: str = Field(default="ai-code-change-agent", pattern=r"^[a-z][a-z0-9-]{1,62}$")
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    log_level: LogLevel = "INFO"
    log_json: bool = True
    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    # ---- database ---------------------------------------------------------------------------
    database_url: SecretStr | None = None
    langgraph_checkpoint_schema: str = "langgraph"  # workflow state, separate from domain tables

    # ---- Jira (webhook intake, read-only REST, optional outcome comment) ---------------------
    jira_base_url: str | None = None
    jira_email: str | None = None
    jira_api_token: SecretStr | None = None
    jira_webhook_secret: SecretStr | None = None
    jira_project_keys: Annotated[frozenset[str], NoDecode] = frozenset()
    jira_story_issue_types: Annotated[frozenset[str], NoDecode] = frozenset({"Story"})
    jira_webhook_event: str = "jira:issue_created"
    jira_webhook_max_body_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    jira_repository_field: str | None = None  # custom field holding the repository reference
    jira_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    jira_read_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    jira_max_retries: int = Field(default=3, ge=0, le=10)
    jira_max_response_bytes: int = Field(default=5_242_880, ge=1024, le=52_428_800)
    jira_comment_page_size: int = Field(default=100, ge=1, le=100)
    jira_max_comments: int = Field(default=500, ge=1, le=5000)
    jira_reporting_enabled: bool = False  # one outcome comment; off unless explicitly enabled

    # ---- GitHub App (installation = external access boundary) --------------------------------
    github_app_id: int | None = Field(default=None, ge=1)
    github_app_installation_id: int | None = Field(default=None, ge=1)
    github_app_private_key_path: str | None = None
    github_pr_create_enabled: bool = False
    code_change_delivery_enabled: bool = False

    # ---- LLM and Deep Agent ---------------------------------------------------------------
    # anthropic: the Claude API for local development and tests.
    # bedrock: AWS Bedrock for staging and production (the only provider allowed there).
    llm_provider: Literal["anthropic", "bedrock"] = "anthropic"
    llm_model: str | None = None
    anthropic_api_key: SecretStr | None = None
    # Bedrock credentials come from the standard AWS chain of the worker (IAM role, profile,
    # environment); never from this file. An optional named profile selects one of them.
    bedrock_region: str | None = None
    bedrock_credentials_profile: str | None = None
    llm_request_timeout_seconds: float = Field(default=180.0, gt=0, le=1800)
    llm_max_retries: int = Field(default=2, ge=0, le=10)
    llm_max_output_tokens: int = Field(default=8192, ge=256, le=128_000)
    # The model's usable input window: drives Deep Agents' summarisation (compress at 85%) and the
    # agent's tool-output trimming (50%).
    llm_context_tokens: int = Field(default=128_000, ge=8_192, le=2_000_000)
    # Client-side pacing of model calls (empty = unlimited).
    llm_requests_per_minute: float | None = Field(default=None, gt=0, le=10_000)
    # Optional JSON price list overriding application/costs/pricing.py (absolute path).
    llm_pricing_file: Path | None = None
    deep_agent_recursion_limit: int = Field(default=250, ge=10, le=5000)
    # Cost safety for any model: model calls and USD per agent phase run (planning,
    # implementation, correction). An attempt has at most 1 + MAX_REPAIR_ATTEMPTS + 1 phases.
    deep_agent_max_model_calls: int = Field(default=80, ge=5, le=1000)
    llm_max_cost_per_phase_usd: float = Field(default=2.0, gt=0, le=1000)
    deep_agent_execute_timeout_seconds: int = Field(default=300, ge=5, le=3600)
    deep_agent_run_timeout_seconds: float = Field(default=5400, ge=60, le=86_400)  # per phase

    # ---- execution sandbox and artifacts ---------------------------------------------------
    workspace_root: str | None = None
    artifact_root: str | None = None
    workspace_image: str | None = None  # the Python execution image (the only toolchain)
    workspace_cpu_limit: float = Field(default=1.0, gt=0, le=16)
    workspace_memory_limit_mb: int = Field(default=2048, ge=128, le=65_536)
    workspace_pids_limit: int = Field(default=256, ge=16, le=4096)
    workspace_disk_limit_mb: int = Field(default=1024, ge=16, le=65_536)
    workspace_timeout_seconds: int = Field(default=7200, ge=60, le=86_400)  # whole sandbox life
    workspace_network_policy: Literal["none"] = "none"  # the only supported policy
    artifact_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    artifact_max_attempt_bytes: int = Field(default=100 * 1024 * 1024, ge=1024, le=1024**3)

    # ---- dependency preparation (ADR-010) ---------------------------------------------------
    dependency_index_url: str = "https://pypi.org/simple"  # the only index the resolver may use
    dependency_max_packages: int = Field(default=60, ge=1, le=500)  # resolved wheels per bundle
    dependency_max_bytes: int = Field(default=300 * 1024 * 1024, ge=1024 * 1024, le=4 * 1024**3)
    dependency_timeout_seconds: float = Field(default=600.0, gt=0, le=3600)
    # accept wheels whose sha256 the index does not publish (recorded "unverified"); off by default
    dependency_allow_unverified_index: bool = False
    # --internal Docker network of the resolver; its only egress is the allow-list proxy
    dependency_resolver_network: str = Field(
        default="aica-dep-resolver", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$"
    )

    # ---- reference fetching (ADR-011): the agent's harness-side web fetch ---------------------
    # Empty list = the tool is not offered. Domains match exactly or as a parent of subdomains.
    reference_allowed_domains: Annotated[frozenset[str], NoDecode] = frozenset()
    reference_max_fetches: int = Field(default=20, ge=1, le=200)  # per agent phase
    # Web search (Anthropic server-side tool, worker side) restricted to the same domains.
    reference_search_enabled: bool = True
    reference_max_searches: int = Field(default=5, ge=1, le=50)  # per agent phase
    reference_max_bytes: int = Field(default=1_048_576, ge=1024, le=16 * 1024 * 1024)
    reference_timeout_seconds: float = Field(default=20.0, gt=0, le=120)

    # ---- LangSmith tracing (worker only; off by default) ---------------------------------------
    # Story text and repository content appear in traces unless the hide flags are set.
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_endpoint: str | None = None  # region or self-hosted API URL (default: US cloud)
    langsmith_workspace_id: str | None = None
    langsmith_agent_id: str | None = (
        None  # agent-based workspace (with LANGSMITH_AGENT_ENVIRONMENT)
    )
    langsmith_agent_environment: Literal["local", "development", "staging", "production"] = "local"
    langsmith_project: str | None = None  # project-based workspace (never with an agent id)
    langsmith_hide_inputs: bool = False
    langsmith_hide_outputs: bool = False

    # ---- jobs and workflow -------------------------------------------------------------------
    # automatic: PLAN and FINAL gates are decided by deterministic policy (hash-bound records,
    # actor policy:automatic). manual: an operator decides with `ai-code-change changes approve`.
    workflow_approval_mode: Literal["automatic", "manual"] = "automatic"
    max_concurrent_jobs: int = Field(default=2, ge=1, le=32)
    job_lease_seconds: int = Field(default=300, ge=30, le=3600)
    job_heartbeat_seconds: int = Field(default=60, ge=5, le=1800)
    job_max_attempts: int = Field(default=3, ge=1, le=10)
    multi_repo_max_repositories: int = Field(default=5, ge=2, le=20)  # per Story
    max_repair_attempts: int = Field(default=1, ge=0, le=3)

    # ---- validators ----------------------------------------------------------------------------
    @field_validator(*_EMPTY_IS_UNSET, mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator(
        "jira_project_keys", "jira_story_issue_types", "reference_allowed_domains", mode="before"
    )
    @classmethod
    def _comma_separated(cls, value: object) -> object:
        return _split_set(value)

    @field_validator("jira_project_keys")
    @classmethod
    def _project_keys(cls, value: frozenset[str]) -> frozenset[str]:
        bad = sorted(v for v in value if not _PROJECT_KEY.fullmatch(v))
        if bad:
            msg = "JIRA_PROJECT_KEYS entries must be Jira project keys (e.g. SHOP)"
            raise ValueError(msg)
        return value

    @field_validator("jira_base_url")
    @classmethod
    def _https(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d{1,5})?/?", value):
            msg = "JIRA_BASE_URL must be an https:// origin without path, query or credentials"
            raise ValueError(msg)
        return value.rstrip("/")

    @field_validator("bedrock_region")
    @classmethod
    def _region(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-[0-9]{1,2}", value):
            msg = "BEDROCK_REGION must be an AWS region name such as us-east-1"
            raise ValueError(msg)
        return value

    @field_validator("bedrock_credentials_profile")
    @classmethod
    def _profile(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_.+=,@-]{1,64}", value):
            msg = "BEDROCK_CREDENTIALS_PROFILE must be a plain AWS profile name"
            raise ValueError(msg)
        return value

    @field_validator("workspace_root", "artifact_root", "github_app_private_key_path")
    @classmethod
    def _absolute(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts:
            msg = "paths must be absolute and must not contain '..'"
            raise ValueError(msg)
        return value

    @field_validator("workspace_image")
    @classmethod
    def _image(cls, value: str | None) -> str | None:
        if value is not None and not _IMAGE.fullmatch(value):
            msg = "WORKSPACE_IMAGE must be a plain image reference"
            raise ValueError(msg)
        return value

    @field_validator("reference_allowed_domains")
    @classmethod
    def _domains(cls, value: frozenset[str]) -> frozenset[str]:
        domain = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
        cleaned = frozenset(v.lower().strip(".") for v in value)
        if any(not domain.fullmatch(v) for v in cleaned):
            msg = "REFERENCE_ALLOWED_DOMAINS entries must be plain domain names (docs.python.org)"
            raise ValueError(msg)
        return cleaned

    @field_validator("dependency_index_url")
    @classmethod
    def _index(cls, value: str) -> str:
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d{1,5})?(/[A-Za-z0-9._~/-]*)?", value):
            msg = "DEPENDENCY_INDEX_URL must be an https:// package index URL without credentials"
            raise ValueError(msg)
        return value.rstrip("/")

    @field_validator("langgraph_checkpoint_schema")
    @classmethod
    def _schema(cls, value: str) -> str:
        if not _SCHEMA.fullmatch(value) or value == "public":
            msg = "LANGGRAPH_CHECKPOINT_SCHEMA must be a lower-case schema name other than public"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _coherent(self) -> "Settings":
        app = [
            self.github_app_id,
            self.github_app_installation_id,
            self.github_app_private_key_path,
        ]
        if any(v is not None for v in app) and not all(v is not None for v in app):
            msg = (
                "GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID and GITHUB_APP_PRIVATE_KEY_PATH must"
                " be set together"
            )
            raise ValueError(msg)
        if self.github_pr_create_enabled and not self.github_app_configured:
            msg = "GITHUB_PR_CREATE_ENABLED=true needs the GitHub App configuration"
            raise ValueError(msg)
        rest = [self.jira_base_url, self.jira_email, self.jira_api_token]
        if any(rest) and not all(rest):
            msg = "JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN must be set together"
            raise ValueError(msg)
        if self.jira_reporting_enabled and not self.jira_rest_configured:
            msg = "JIRA_REPORTING_ENABLED=true needs JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN"
            raise ValueError(msg)
        if self.langsmith_tracing and self.langsmith_api_key is None:
            msg = "LANGSMITH_TRACING=true needs LANGSMITH_API_KEY"
            raise ValueError(msg)
        if self.langsmith_agent_id and self.langsmith_project:
            msg = "set LANGSMITH_AGENT_ID or LANGSMITH_PROJECT, not both"
            raise ValueError(msg)
        if self.langsmith_endpoint and not self.langsmith_endpoint.startswith("https://"):
            msg = "LANGSMITH_ENDPOINT must be an https:// URL"
            raise ValueError(msg)
        if self.job_heartbeat_seconds * 2 > self.job_lease_seconds:
            msg = "JOB_HEARTBEAT_SECONDS must be at most half of JOB_LEASE_SECONDS"
            raise ValueError(msg)
        if self.app_env in ("staging", "production"):
            if self.llm_provider != "bedrock":
                msg = f"{self.app_env} requires LLM_PROVIDER=bedrock (anthropic is for local tests)"
                raise ValueError(msg)
            missing = [
                name
                for name, ok in (
                    ("DATABASE_URL", self.database_url is not None),
                    ("JIRA_WEBHOOK_SECRET", self.jira_webhook_secret is not None),
                    ("JIRA_PROJECT_KEYS", bool(self.jira_project_keys)),
                )
                if not ok
            ]
            if missing:
                msg = f"{self.app_env} requires: {', '.join(missing)}"
                raise ValueError(msg)
        return self

    # ---- derived --------------------------------------------------------------------------
    @property
    def jira_webhook_configured(self) -> bool:
        return self.jira_webhook_secret is not None and bool(self.jira_project_keys)

    @property
    def jira_rest_configured(self) -> bool:
        return bool(self.jira_base_url and self.jira_email and self.jira_api_token)

    @property
    def github_app_configured(self) -> bool:
        return (
            self.github_app_id is not None
            and self.github_app_installation_id is not None
            and self.github_app_private_key_path is not None
        )

    @property
    def llm_configured(self) -> bool:
        if self.llm_provider == "bedrock":
            return self.llm_model is not None and self.bedrock_region is not None
        return self.llm_model is not None and self.anthropic_api_key is not None


@lru_cache
def get_settings() -> Settings:
    return Settings()
