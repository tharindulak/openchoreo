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
    rca_llm_api_key: str = ""

    observer_api_url: str = "http://observer:8080"
    openchoreo_api_url: str = "http://openchoreo-api.openchoreo-control-plane.svc.cluster.local:8080"
    ae_api_url: str = ""

    @property
    def observer_mcp_url(self) -> str:
        return f"{self.observer_api_url.rstrip('/')}/mcp"

    @property
    def openchoreo_mcp_url(self) -> str:
        return f"{self.openchoreo_api_url.rstrip('/')}/mcp"

    @property
    def ae_mcp_url(self) -> str:
        return f"{self.ae_api_url.rstrip('/')}/mcp"

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
    remed_agent: bool = False
    ae_handoff: bool = False
    ae_auto_dispatch: bool = True

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


settings = Settings()
