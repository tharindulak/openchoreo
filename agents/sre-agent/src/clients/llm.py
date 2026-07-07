# Copyright 2025 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from src.config import settings

logger = logging.getLogger(__name__)


def resolve_api_key() -> str:
    """Returns the current Anthropic API key. Call this fresh in every
    Agent.create() invocation (NOT once at import time) — that's what makes
    a console-side key rotation take effect on the next analysis/chat
    request without restarting this pod.

    Prefers RCA_LLM_API_KEY_FILE: a K8s Secret volume mount whose content
    an ExternalSecret keeps synced from OpenBao (secret/aep/<org>/anthropic/
    key), the same value the AE console's "Connect Anthropic key" flow
    writes. Falls back to the static RCA_LLM_API_KEY env var — read once at
    process start — when the file isn't configured or can't be read, so
    deployments without the ExternalSecret wiring behave exactly as before.
    """
    file_path = settings.rca_llm_api_key_file
    if file_path:
        try:
            key = Path(file_path).read_text().strip()
        except OSError as e:
            logger.warning(
                "Failed to read RCA_LLM_API_KEY_FILE %s (%s) — falling back to RCA_LLM_API_KEY",
                file_path,
                e,
            )
        else:
            if key:
                return key
            logger.warning(
                "RCA_LLM_API_KEY_FILE %s is empty — falling back to RCA_LLM_API_KEY", file_path
            )
    return settings.rca_llm_api_key


@lru_cache
def get_model(
    model_name: str = settings.rca_model_name,
    api_key: str = settings.rca_llm_api_key,
    **kwargs: Any,
) -> BaseChatModel:
    # lru_cache keys on the actual argument values, not on whether they were
    # explicitly passed — callers that pass a freshly resolved api_key (see
    # resolve_api_key) get a cache hit when the key is unchanged and a fresh
    # ChatAnthropic client only when it actually rotates.
    #
    # Fail fast on a stalled request instead of inheriting the provider SDK's
    # ~10-min default: a short per-request timeout + retries turns a wedged
    # LLM call into a quick retry rather than a hung analysis. Callers can
    # still override by passing timeout/max_retries explicitly.
    kwargs.setdefault("timeout", settings.llm_request_timeout_seconds)
    kwargs.setdefault("max_retries", settings.llm_max_retries)
    return init_chat_model(model=model_name, api_key=api_key, **kwargs)
