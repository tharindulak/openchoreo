# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

from src.clients.sink.report_sink import ReportSink, WebhookReportSink, should_publish_report
from src.config import settings


def get_report_sink() -> ReportSink | None:
    """The configured sink, or None when reports are not published anywhere.

    None rather than a no-op sink: "there is no sink" is the default and the
    caller logs it differently from a sink that ran, so a silent null object
    would hide a misconfigured deployment behind a working-looking one.
    """
    kind = settings.report_sink.strip()
    if not kind:
        return None
    if kind == "webhook":
        return WebhookReportSink(settings.report_sink_url)
    raise ValueError(f"Unknown report sink: {kind!r}. Use 'webhook', or leave it unset.")


__all__ = [
    "ReportSink",
    "WebhookReportSink",
    "get_report_sink",
    "should_publish_report",
]
