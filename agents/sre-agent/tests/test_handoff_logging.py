# Copyright 2026 The OpenChoreo Authors
# SPDX-License-Identifier: Apache-2.0

"""Obsolete: `_handoff_log_fields` and `HandoffClassification` are gone.

`HandoffResult` became fully generic (`tool`, `result`, `failure_reason`) in
the commit that deleted `handoff_view()`, and Task 10 deleted
`_handoff_log_fields` itself (`run_analysis` now logs `handoff_report.tool`
and `.result` directly — see `src/agent/agent.py`). This file was left behind
importing both; nothing here is left to test. Left as an empty module rather
than removed outright because this sandbox's file-deletion permission denied
`rm`/`git rm` — a maintainer should `git rm tests/test_handoff_logging.py`.
"""
