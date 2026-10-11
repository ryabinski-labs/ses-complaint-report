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
        redact_emails=_bool("SESREPORT_REDACT_EMAILS", False),
        demo=_bool("SESREPORT_DEMO", False),
        host=os.environ.get("SESREPORT_HOST", "127.0.0.1"),
        port=int(os.environ.get("SESREPORT_PORT", "8787")),
        refresh_seconds=int(os.environ.get("SESREPORT_REFRESH_SECONDS", "3600")),
    )
