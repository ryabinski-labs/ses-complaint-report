"""FastAPI service: serves the interactive report and refreshes AWS data."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from .collect import collect_all
from .config import load as load_config
from .demo import demo_model

log = logging.getLogger("sesreport")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

HERE = Path(__file__).resolve().parent
CFG = load_config()
DATA_DIR = Path(os.environ.get("SESREPORT_DATA_DIR", HERE / "data"))
CACHE_FILE = DATA_DIR / "latest.json"
HISTORY_FILE = DATA_DIR / "history.jsonl"
HISTORY_MAX = int(os.environ.get("SESREPORT_HISTORY_MAX", 500))
REFRESH_INTERVAL_S = CFG.refresh_seconds
HOST = CFG.host
PORT = CFG.port


def _snapshot(model: dict) -> dict:
    """A small, comparable summary of one collection run, for change detection."""
    met = model.get("metrics", {})
    sl = model.get("suppression_list") or {}
    return {
        "generated_at": model.get("meta", {}).get("generated_at"),
        "total_complaints": met.get("total_complaints"),
        "total_sends": met.get("total_sends"),
        "overall_rate": met.get("overall_complaint_rate"),
        "latest_rate": (met.get("latest_rate") or {}).get("rate"),
        "peak_rate": (met.get("peak_rate") or {}).get("rate"),
        "suppression_complaints": sl.get("complaints"),
        "suppression_total": sl.get("total"),
        "complainants": sorted(
            c.get("email", "") for c in model.get("complainants", []) if c.get("email")
        ),
        "complaint_days": [d.get("date") for d in met.get("complaints_by_day", [])],
        "config_set_events": {
            c["config_set"]: c["events"] for c in model.get("config_set_complaints", [])
        },
    }


def _append_history(model: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    snap = _snapshot(model)
    try:
        lines = HISTORY_FILE.read_text().splitlines() if HISTORY_FILE.exists() else []
        lines.append(json.dumps(snap, default=str))
        HISTORY_FILE.write_text("\n".join(lines[-HISTORY_MAX:]) + "\n")
    except OSError as e:
        log.warning("could not write history: %s", e)


def _read_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    out = []
    for line in HISTORY_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out

STATE_LOCK = threading.Lock()
STATE = {
    "model": None,
    "refreshing": False,
    "last_started": None,
    "last_finished": None,
    "last_error": None,
    "last_error_at": None,
}


def _load_cache() -> None:
    if CACHE_FILE.exists():
        try:
            STATE["model"] = json.loads(CACHE_FILE.read_text())
            STATE["last_finished"] = STATE["model"].get("meta", {}).get("generated_at")
            log.info("loaded cached report from %s", CACHE_FILE)
            if not _read_history():
                _append_history(STATE["model"])
        except (ValueError, OSError) as e:
            log.warning("could not read cache: %s", e)


def _do_refresh() -> dict:
    STATE["last_started"] = dt.datetime.now(dt.timezone.utc).isoformat()
    log.info("refresh started")
    t0 = time.time()
    try:
        model = demo_model() if CFG.demo else collect_all()
    except Exception as e:  # noqa: BLE001 - surface any failure to the UI
        log.exception("refresh failed")
        STATE["last_error"] = f"{type(e).__name__}: {e}"
        STATE["last_error_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        raise
    if CFG.demo:
        model["meta"]["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    model["meta"]["served_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(model, default=str))
    tmp.replace(CACHE_FILE)
    with STATE_LOCK:
        STATE["model"] = model
        STATE["last_error"] = None
    STATE["last_finished"] = model["meta"]["generated_at"]
    _append_history(model)
    log.info("refresh done in %.1fs (%d errors)", time.time() - t0, len(model.get("errors", [])))
    return model


def _refresh_worker():
    with STATE_LOCK:
        if STATE["refreshing"]:
            return
        STATE["refreshing"] = True
    try:
        _do_refresh()
    except Exception:  # noqa: BLE001
        pass
    finally:
        STATE["refreshing"] = False


def _hourly_loop():
    while True:
        time.sleep(REFRESH_INTERVAL_S)
        try:
            _refresh_worker()
        except Exception:  # noqa: BLE001
            log.exception("hourly refresh loop error")


def _start_background() -> None:
    threading.Thread(target=_hourly_loop, daemon=True, name="hourly-refresh").start()


app = FastAPI(title="SES Complaint Report")


@app.on_event("startup")
def _startup():
    _load_cache()
    _start_background()
    # always kick a fresh collection so the first page load is current
    threading.Thread(target=_refresh_worker, daemon=True, name="initial-refresh").start()


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/favicon.svg")
def favicon_svg():
    return FileResponse(HERE / "static" / "favicon.svg", media_type="image/svg+xml")


@app.get("/favicon.ico")
def favicon_ico():
    return FileResponse(HERE / "static" / "favicon.svg", media_type="image/svg+xml")


@app.get("/api/report")
def report():
    model = STATE["model"]
    if model is None:
        status = _status_payload()
        return JSONResponse(
            status_code=202,
            content={"status": "collecting", **status},
        )
    return JSONResponse(content=model)


@app.post("/api/refresh")
def refresh():
    with STATE_LOCK:
        if STATE["refreshing"]:
            return {"status": "already-refreshing", **_status_payload()}
    threading.Thread(target=_refresh_worker, daemon=True, name="manual-refresh").start()
    return {"status": "started"}


def _status_payload() -> dict:
    model = STATE["model"]
    return {
        "refreshing": STATE["refreshing"],
        "last_finished": STATE["last_finished"],
        "last_started": STATE["last_started"],
        "last_error": STATE["last_error"],
        "last_error_at": STATE["last_error_at"],
        "has_data": model is not None,
        "generated_at": (model or {}).get("meta", {}).get("generated_at"),
        "collect_ms": (model or {}).get("collect_ms"),
        "refresh_interval_s": REFRESH_INTERVAL_S,
    }


@app.get("/api/status")
def status():
    return _status_payload()


@app.get("/api/history")
def history():
    return {"entries": _read_history()}


def main():
    if CFG.demo:
        log.info("DEMO mode — serving synthetic data (no AWS calls)")
    log.info("starting on http://%s:%d (refresh every %ds)", HOST, PORT, REFRESH_INTERVAL_S)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
