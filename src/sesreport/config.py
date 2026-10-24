# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski

"""Runtime configuration, read from environment (and an optional .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _split(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _load_dotenv() -> None:
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file)
    except ImportError:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    profile: str = "default"
    region: str = "us-east-1"
    extra_regions: list[str] = field(default_factory=lambda: ["us-west-2"])
    log_groups: list[str] = field(default_factory=list)
    log_group_prefix: str = "/aws/lambda/"
    log_group_match: str = ""
    # Application stores. These are app-specific; leave blank to skip the
    # per-recipient sections entirely (the demo model covers the showcase).
    complaint_table: str = ""
    complaint_attr: str = "bounce_type"
    complaint_value: str = "Complaint"
    complaint_queue: str = ""
    suppression_tables: list[str] = field(default_factory=list)
    ledger_table: str = ""
    # Identities tagged Project=<one of these> are DKIM-signed by the sending
    # app before SES (Folio/WebMail signs in-app), so SES's own DKIM status
    # is expected to be NOT_STARTED for them.
    app_dkim_projects: list[str] = field(default_factory=lambda: ["WebMail"])
    # Domains whose MX points at one of these hosts are served by an in-app
    # signer too (Folio/WebMail inbound), for identities that predate tagging.
    app_dkim_mx: list[str] = field(default_factory=lambda: ["inbound.wm.emcognito.com"])
    # Identities that only send QA/test mail: auth gaps there are notes, not risks.
    test_identities: list[str] = field(default_factory=list)
    # Open/click tracking rewrites links; it is off by policy unless this is set,
    # so missing OPEN/CLICK publishing is never a finding by default.
    engagement_tracking: bool = False
    # The ledger ingest Lambda's function name: an SNS topic with a lambda
    # subscription ending in this is "ledger-subscribed".
    ledger_function: str = "ses-complaint-report-ledger-ingest"
    redact_emails: bool = False
    demo: bool = False
    host: str = "127.0.0.1"
    port: int = 8787
    refresh_seconds: int = 3600


def load() -> Config:
    _load_dotenv()
    return Config(
        profile=os.environ.get("SESREPORT_PROFILE", "default"),
        region=os.environ.get("SESREPORT_REGION", "us-east-1"),
        extra_regions=_split(os.environ.get("SESREPORT_EXTRA_REGIONS", "us-west-2")),
        log_groups=_split(os.environ.get("SESREPORT_LOG_GROUPS", "")),
        log_group_prefix=os.environ.get("SESREPORT_LOG_GROUP_PREFIX", "/aws/lambda/"),
        log_group_match=os.environ.get("SESREPORT_LOG_GROUP_MATCH", ""),
        complaint_table=os.environ.get("SESREPORT_COMPLAINT_TABLE", ""),
        complaint_attr=os.environ.get("SESREPORT_COMPLAINT_ATTR", "bounce_type"),
        complaint_value=os.environ.get("SESREPORT_COMPLAINT_VALUE", "Complaint"),
        complaint_queue=os.environ.get("SESREPORT_COMPLAINT_QUEUE", ""),
        suppression_tables=_split(os.environ.get("SESREPORT_SUPPRESSION_TABLES", "")),
        ledger_table=os.environ.get("SESREPORT_LEDGER_TABLE", ""),
        app_dkim_projects=_split(os.environ.get("SESREPORT_APP_DKIM_PROJECTS", "WebMail")),
        app_dkim_mx=_split(os.environ.get("SESREPORT_APP_DKIM_MX", "inbound.wm.emcognito.com")),
        test_identities=_split(os.environ.get("SESREPORT_TEST_IDENTITIES", "")),
        engagement_tracking=_bool("SESREPORT_ENGAGEMENT_TRACKING", False),
        ledger_function=os.environ.get("SESREPORT_LEDGER_FUNCTION", "ses-complaint-report-ledger-ingest"),
        redact_emails=_bool("SESREPORT_REDACT_EMAILS", False),
        demo=_bool("SESREPORT_DEMO", False),
        host=os.environ.get("SESREPORT_HOST", "127.0.0.1"),
        port=int(os.environ.get("SESREPORT_PORT", "8787")),
        refresh_seconds=int(os.environ.get("SESREPORT_REFRESH_SECONDS", "3600")),
    )
