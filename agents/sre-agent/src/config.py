# Copyright 2025 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LABEL_ENVIRONMENT_UID = "openchoreo.dev/environment-uid"
LABEL_PROJECT_UID = "openchoreo.dev/project-uid"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="allow",
    )

    rca_model_name: str = ""
    # Static fallback — read once at process start. Prefer
    # rca_llm_api_key_file when set: it points at a K8s Secret volume mount
    # kept live-synced (via ExternalSecret from OpenBao) with the org's
    # Anthropic key as set through the AE console, so rotations take effect
    # without restarting this pod. See src/clients/llm.py:resolve_api_key.
    rca_llm_api_key: str = ""
    rca_llm_api_key_file: str = ""

    observer_api_url: str = "http://observer:8080"
    openchoreo_api_url: str = (
        "http://openchoreo-api.openchoreo-control-plane.svc.cluster.local:8080"
    )
    ae_api_url: str = ""
    # Path of the handoff MCP endpoint under ae_api_url. Two deployment shapes
    # exist for the ae_* handoff tools and they answer on DIFFERENT paths:
    #   - the standalone aep-mcp-server serves them at "/mcp" (default here), and
    #   - the in-process aep-api surface serves them at "/sre-mcp".
    # ae_mcp_url = ae_api_url + ae_mcp_path, so point ae_api_url at whichever
    # server is deployed and set ae_mcp_path to match. Default "/mcp" tracks the
    # standalone aep-mcp-server that current installs run (see AE-HANDOFF-DESIGN.md).
    ae_mcp_path: str = "/mcp"
    # aep-api's REST base for publishing RCA reports (POST /api/v1/rca-agent/
    # reports). ae_api_url is the base for the handoff MCP surface (see
    # ae_mcp_path), and aep_api_url is the base for report publishing — both
    # target the AEP side; aep_api_url falls back to ae_api_url when unset. See
    # RCA-REPORT-PUBLISHING.md.
    aep_api_url: str = ""

    @property
    def rca_reports_api_base(self) -> str:
        """Base URL for aep-api's RCA-report REST endpoint (aep_api_url,
        falling back to ae_api_url)."""
        return (self.aep_api_url or self.ae_api_url).rstrip("/")

    @property
    def observer_mcp_url(self) -> str:
        return f"{self.observer_api_url.rstrip('/')}/mcp"

    @property
    def openchoreo_mcp_url(self) -> str:
        return f"{self.openchoreo_api_url.rstrip('/')}/mcp"

    @property
    def ae_mcp_url(self) -> str:
        # Handoff MCP endpoint = base + configurable path (see ae_mcp_path).
        # Default path "/mcp" matches the standalone aep-mcp-server; set
        # AE_MCP_PATH=/sre-mcp for the in-process aep-api surface.
        return f"{self.ae_api_url.rstrip('/')}/{self.ae_mcp_path.strip('/')}"

    report_backend: str = "sqlite"
    sql_backend_uri: str = ""

    oauth_token_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_scope: str = ""
    jwt_jwks_url: str = ""
    jwt_issuer: str = ""
    jwt_audience: str = ""
    jwt_jwks_refresh_interval: int = 3600
    authz_timeout_seconds: int = 30
    auth_config_path: str = "auth-config.yaml"

    @property
    def authz_service_url(self) -> str:
        return self.openchoreo_api_url.rstrip("/")

    max_concurrent_analyses: int = 5
    analysis_timeout_seconds: int = 1500
    # Per-LLM-request cap (seconds) and retry count. Without these the
    # Anthropic SDK defaults apply (~600s/request), so one stalled request
    # can wedge an analysis for ~10min before failing. A short per-request
    # timeout + retries fails fast on a hung request and recovers on the
    # next attempt instead of stalling the whole run.
    llm_request_timeout_seconds: int = 120
    llm_max_retries: int = 2
    # MCP get_tools() retry: opening connections to all MCP servers is a single
    # task-group call that fails whole if any one server is transiently slow
    # (CPU-starved node, OAuth fetch timeout). Retrying rescued handoff runs
    # that used to die outright when one server flaked.
    mcp_get_tools_max_retries: int = 3
    mcp_get_tools_retry_backoff_seconds: float = 2.0
    # Directory of deploy-time-materialized skills, searched BEFORE the built-in
    # src/skills library so a mounted skill overrides or adds to it. The handoff
    # skill 'issue-fix' is owned by AEP (canonical home:
    # services/aep-mcp-server/skills/issue-fix in labs-agentic-engineer) and is
    # mounted here via a ConfigMap at deploy time — AEP is the source of truth,
    # so it is NOT baked into this image. In-cluster this points at the mount
    # (e.g. /etc/rca-agent/skills); for local dev point it at a checked-out copy.
    # Empty ⇒ only the built-in library is used (handoff skill will be missing).
    external_skills_dir: str = ""
    remed_agent: bool = False
    ae_handoff: bool = False
    ae_auto_dispatch: bool = True
    # When true, each completed RCA report is POSTed to aep-api's
    # create-rca-agent-report endpoint so it surfaces in the AE console's
    # Alerts bell/list (labs-agentic-engineer #154/#155/#156, PR #161).
    # Requires aep_api_url (the aep-api REST base) plus the OAUTH_*
    # client-credentials config the handoff already uses.
    # See RCA-REPORT-PUBLISHING.md.
    ae_publish_reports: bool = False

    log_level: str = "INFO"
    openai_debug_logs: bool = False
    tls_insecure_skip_verify: bool = False
    cors_allowed_origins: str = ""

    @model_validator(mode="after")
    def _validate_backend_config(self) -> Settings:
        if self.report_backend == "postgresql" and not self.sql_backend_uri:
            raise ValueError("report_backend='postgresql' requires: sql_backend_uri")
        if self.report_backend == "sqlite" and not self.sql_backend_uri:
            self.sql_backend_uri = "sqlite+aiosqlite:///data/rca_reports.db"
        if self.sql_backend_uri and not self.sql_backend_uri.startswith(self.report_backend):
            raise ValueError(
                f"sql_backend_uri scheme must match report_backend='{self.report_backend}'"
            )
        return self

    @model_validator(mode="after")
    def _validate_ae_handoff_config(self) -> Settings:
        if self.ae_handoff and not self.ae_api_url:
            raise ValueError("ae_handoff=True requires: ae_api_url")
        return self

    @model_validator(mode="after")
    def _validate_ae_publish_reports_config(self) -> Settings:
        if self.ae_publish_reports and not self.rca_reports_api_base:
            raise ValueError("ae_publish_reports=True requires: aep_api_url (or ae_api_url)")
        return self


settings = Settings()
