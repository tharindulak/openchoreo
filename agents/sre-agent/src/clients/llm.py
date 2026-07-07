# Copyright 2025 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

from functools import lru_cache
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from src.config import settings


@lru_cache
def get_model(
    model_name: str = settings.rca_model_name,
    api_key: str = settings.rca_llm_api_key,
    **kwargs: Any,
) -> BaseChatModel:
    # Fail fast on a stalled request instead of inheriting the provider SDK's
    # ~10-min default: a short per-request timeout + retries turns a wedged
    # LLM call into a quick retry rather than a hung analysis. Callers can
    # still override by passing timeout/max_retries explicitly.
    kwargs.setdefault("timeout", settings.llm_request_timeout_seconds)
    kwargs.setdefault("max_retries", settings.llm_max_retries)
    return init_chat_model(model=model_name, api_key=api_key, **kwargs)
