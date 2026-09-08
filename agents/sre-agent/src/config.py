# Copyright 2025 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

from pydantic import model_validator
from pydantic_settings import SettingsConfigDict

from common.config import CommonSettings

LABEL_ENVIRONMENT_UID = "openchoreo.dev/environment-uid"
LABEL_PROJECT_UID = "openchoreo.dev/project-uid"


class Settings(CommonSettings):
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
    # Set to route LLM calls through an OpenAI-compatible gateway; see
    # src/clients/llm.py:get_model.
    rca_llm_base_url: str = ""

    observer_api_url: str = "http://observer:8080"
    # openchoreo_api_url now comes from CommonSettings (same default).
    handoff_api_url: str = ""
    # Path of the handoff MCP endpoint under handoff_api_url. A platform may
    # serve its handoff tools from a standalone MCP server or from an endpoint on
    # its main API, and those answer on different paths, so the base and the path
    # are configured separately: handoff_mcp_url = handoff_api_url +
    # handoff_mcp_path.
    handoff_mcp_path: str = "/mcp"

    @property
    def observer_mcp_url(self) -> str:
        return f"{self.observer_api_url.rstrip('/')}/mcp"

    @property
    def openchoreo_mcp_url(self) -> str:
        return f"{self.openchoreo_api_url.rstrip('/')}/mcp"

    @property
    def handoff_mcp_url(self) -> str:
        # Handoff MCP endpoint = base + configurable path (see ae_mcp_path).
        # Default path "/mcp" matches the standalone aep-mcp-server; set
        # AE_MCP_PATH=/sre-mcp for the in-process aep-api surface.
        return f"{self.handoff_api_url.rstrip('/')}/{self.handoff_mcp_path.strip('/')}"

    report_backend: str = "sqlite"
    sql_backend_uri: str = ""

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
    # skill 'coding-agent-handoff' is owned by AEP (canonical home:
    # services/aep-mcp-server/skills/coding-agent-handoff in labs-agentic-engineer) and is
    # mounted here via a ConfigMap at deploy time — AEP is the source of truth,
    # so it is NOT baked into this image. In-cluster this points at the mount
    # (e.g. /etc/rca-agent/skills); for local dev point it at a checked-out copy.
    # Empty ⇒ only the built-in library is used (handoff skill will be missing).
    external_skills_dir: str = ""
    remed_agent: bool = False
    handoff_enabled: bool = False
    # Where completed reports go BESIDES report_backend — a downstream system
    # that wants to know an analysis finished (a platform console, a tracker, an
    # event bus). Empty means nowhere, which is the default: an agent publishes
    # only where it is told to. "webhook" POSTs the report to report_sink_url
    # with the agent's own OAUTH_* service-account credentials, the same ones its
    # other outbound calls use. The receiver maps the report to its own schema;
    # this agent sends the report as it models it. See src/clients/sink/.
    report_sink: str = ""
    report_sink_url: str = ""

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

    # Names the receiving platform ships for the tools it exposes and the
    # answers it gives (src/agent/handoff_provider.py). Mounted at deploy time
    # like the handoff skill, because the platform owns both.
    handoff_provider_file: str = ""

    @model_validator(mode="after")
    def _validate_handoff_config(self) -> Settings:
        if not self.handoff_enabled:
            return self
        missing = [
            name
            for name, value in (
                ("handoff_api_url", self.handoff_api_url),
                ("handoff_provider_file", self.handoff_provider_file),
                ("external_skills_dir", self.external_skills_dir),
            )
            if not value
        ]
        if missing:
            # Loud, at startup. Without the descriptor the agent would still
            # run, still reach the model, and still call the create tool — with
            # none of the facts pinned onto it. That files an issue nothing can
            # dedupe and nobody hands over, and it looks like success.
            #
            # The skills mount fails the same way one layer earlier: the stage's
            # whole playbook is the mounted skill, so without it load_skills
            # raises once per incident, the broad handler in run_analysis saves
            # the report with no handoff key, and "the loader broke" is
            # indistinguishable from "nothing needed handing over".
            raise ValueError(f"handoff_enabled=True requires: {', '.join(missing)}")
        return self

    @model_validator(mode="after")
    def _validate_report_sink_config(self) -> Settings:
        if self.report_sink and not self.report_sink_url:
            raise ValueError(f"report_sink={self.report_sink!r} requires: report_sink_url")
        return self


settings = Settings()
