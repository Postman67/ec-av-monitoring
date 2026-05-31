#!/usr/bin/env python3
"""
Prometheus exporter for Bitfocus Companion module variables.

Polls the Companion HTTP REST API for ATEM, HD8, and OBS variable values,
plus connection health status for all enabled modules.

Exposed on port 9123.

Usage:
  python companion_exporter.py
  COMPANION_HOST=10.50.0.10 COMPANION_PORT=8000 python companion_exporter.py
"""

import os
import time
import logging
import signal
import sys
import requests
from prometheus_client import start_http_server, Gauge, Info, Enum

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

COMPANION_HOST = os.environ.get("COMPANION_HOST", "10.50.0.10")
COMPANION_PORT = int(os.environ.get("COMPANION_PORT", "8000"))
METRICS_PORT = int(os.environ.get("COMPANION_METRICS_PORT", "9123"))
POLL_INTERVAL = int(os.environ.get("COMPANION_POLL_INTERVAL", "10"))  # seconds
REQUEST_TIMEOUT = 5  # seconds

BASE_URL = f"http://{COMPANION_HOST}:{COMPANION_PORT}"

# Variables to scrape: (connection_label, variable_name, metric_type)
# metric_type: "gauge_float", "gauge_int", "string"
VARIABLES = [
    # ATEM
    ("ATEM", "pgm1_input", "string"),
    ("ATEM", "pvw1_input", "string"),
    ("ATEM", "aux1_input", "string"),
    # HD8 (streaming ATEM)
    ("HD8", "pgm1_input", "string"),
    ("HD8", "pvw1_input", "string"),
    ("HD8", "aux1_input", "string"),
    ("HD8", "aux2_input", "string"),
    ("HD8", "aux3_input", "string"),
    ("HD8", "stream_bitrate", "gauge_float"),
    # OBS
    ("PC_obs", "recording", "string"),
    ("PC_obs", "streaming", "string"),
    ("PC_obs", "fps", "gauge_float"),
    ("PC_obs", "stream_timecode", "string"),
    ("PC_obs", "profile", "string"),
    ("PC_obs", "scene_collection", "string"),
]

# Connections to monitor health for
CONNECTION_LABELS = [
    "PC_obs", "ATEM", "HD8", "WING",
    "VOX1", "VOX2", "VOX3", "VOX4", "VOX5", "VOX6", "VOX7", "VOX8",
    "PJ_L", "PJ_R", "PP7-API",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("companion_exporter")

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

# Connection health
CONNECTION_HEALTH = Gauge(
    "companion_connection_healthy",
    "Connection health status (1=ok, 0=error/disabled)",
    ["label", "module"],
)

CONNECTION_ENABLED = Gauge(
    "companion_connection_enabled",
    "Whether connection is enabled (1=enabled)",
    ["label", "module"],
)

CONNECTION_STATUS_MESSAGE = Info(
    "companion_connection_status",
    "Connection status details",
    ["label"],
)

# Variable values (as strings via Info)
VARIABLE_VALUE = Info(
    "companion_variable",
    "Companion module variable value",
    ["connection", "variable"],
)

# Numeric variable values (gauges)
VARIABLE_GAUGE = Gauge(
    "companion_variable_numeric",
    "Companion module numeric variable value",
    ["connection", "variable"],
)

# Derived metrics for easy dashboarding
ATEM_PROGRAM_INPUT = Info("atem_program_input", "ATEM program input", ["device"])
ATEM_PREVIEW_INPUT = Info("atem_preview_input", "ATEM preview input", ["device"])
OBS_RECORDING = Gauge("obs_recording_active", "OBS recording active (1=Recording)")
OBS_STREAMING = Gauge("obs_streaming_active", "OBS streaming active (1=Streaming)")
OBS_FPS = Gauge("obs_fps", "OBS current FPS")
HD8_STREAM_BITRATE = Gauge("hd8_stream_bitrate_mbps", "HD8 streaming bitrate in Mbps")


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class CompanionCollector:
    """Polls Companion REST API and updates Prometheus metrics."""

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def _get(self, path: str, json=True):
        """Make a GET request. Returns parsed JSON or raw text depending on `json` flag."""
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                text = resp.text.strip()
                if json:
                    try:
                        return resp.json()
                    except ValueError:
                        return text
                return text
            elif resp.status_code == 404:
                return None
            else:
                log.warning("GET %s returned %d: %s", path, resp.status_code, resp.text[:200])
                return None
        except requests.RequestException as exc:
            log.warning("GET %s failed: %s", path, exc)
            return None

    def poll_connections(self):
        """Poll /api/connections for health status."""
        data = self._get("/api/connections")
        if not data:
            log.warning("Failed to fetch connections")
            return

        for conn in data:
            label = conn.get("label", "")
            module = conn.get("moduleId", "")
            enabled = conn.get("enabled", False)
            status = conn.get("status") or {}
            category = status.get("category", "unknown")

            if label in CONNECTION_LABELS:
                CONNECTION_ENABLED.labels(label=label, module=module).set(1 if enabled else 0)
                if enabled and category == "good":
                    CONNECTION_HEALTH.labels(label=label, module=module).set(1)
                else:
                    CONNECTION_HEALTH.labels(label=label, module=module).set(0)

                msg = status.get("message") or category or "unknown"
                CONNECTION_STATUS_MESSAGE.labels(label=label).info({
                    "module": module,
                    "category": category,
                    "message": str(msg)[:100],
                })

    def poll_variables(self):
        """Poll each variable from the configured list."""
        for conn_label, var_name, var_type in VARIABLES:
            path = f"/api/variable/{conn_label}/{var_name}/value"
            data = self._get(path, json=False)

            if data is None:
                continue

            # The endpoint returns plain text (e.g. "5.85", "Recording", "Off-Air")
            # Skip "Not found" responses
            value = str(data).strip()
            if value.lower() in ("not found", "null", "", "undefined"):
                continue

            # Store as string info metric
            VARIABLE_VALUE.labels(connection=conn_label, variable=var_name).info(
                {"value": str(value)[:200]}
            )

            # Store as numeric gauge if applicable
            if var_type in ("gauge_float", "gauge_int"):
                try:
                    num = float(value) if var_type == "gauge_float" else int(value)
                    VARIABLE_GAUGE.labels(
                        connection=conn_label, variable=var_name
                    ).set(num)
                except (ValueError, TypeError):
                    log.debug(
                        "Non-numeric value for %s/%s: %s", conn_label, var_name, value
                    )

            # Set derived convenience metrics
            self._set_derived(conn_label, var_name, value)

    def _set_derived(self, conn_label: str, var_name: str, value):
        """Set convenience/derived metrics for common queries."""
        # ATEM inputs
        if conn_label == "ATEM" and var_name == "pgm1_input":
            ATEM_PROGRAM_INPUT.labels(device="ATEM").info({"input": str(value)})
        elif conn_label == "ATEM" and var_name == "pvw1_input":
            ATEM_PREVIEW_INPUT.labels(device="ATEM").info({"input": str(value)})
        elif conn_label == "HD8" and var_name == "pgm1_input":
            ATEM_PROGRAM_INPUT.labels(device="HD8").info({"input": str(value)})
        elif conn_label == "HD8" and var_name == "pvw1_input":
            ATEM_PREVIEW_INPUT.labels(device="HD8").info({"input": str(value)})

        # OBS recording/streaming
        elif conn_label == "PC_obs" and var_name == "recording":
            OBS_RECORDING.set(1 if str(value).lower() == "recording" else 0)
        elif conn_label == "PC_obs" and var_name == "streaming":
            OBS_STREAMING.set(1 if str(value).lower() not in ("off-air", "off", "false", "0") else 0)
        elif conn_label == "PC_obs" and var_name == "fps":
            try:
                OBS_FPS.set(float(value))
            except (ValueError, TypeError):
                pass

        # HD8 stream bitrate
        elif conn_label == "HD8" and var_name == "stream_bitrate":
            try:
                HD8_STREAM_BITRATE.set(float(value))
            except (ValueError, TypeError):
                pass

    def collect_all(self):
        """Run a full collection cycle."""
        self.poll_connections()
        self.poll_variables()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("Starting Companion exporter on :%d (polling %s)", METRICS_PORT, BASE_URL)
    start_http_server(METRICS_PORT)
    log.info("Metrics available at http://0.0.0.0:%d/metrics", METRICS_PORT)

    collector = CompanionCollector(BASE_URL)

    def _shutdown(signum, frame):
        log.info("Received signal %d – shutting down…", signum)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while True:
        try:
            collector.collect_all()
        except Exception as exc:
            log.error("Collection error: %s", exc)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
