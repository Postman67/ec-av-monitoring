#!/usr/bin/env python3
"""
OBS Studio Prometheus Exporter

Connects to OBS WebSocket v5 (10.50.0.4:4455) and exposes metrics on port 9121.
Handles challenge/salt authentication, auto-reconnect, and robust error handling.

Config via env vars:
  OBS_HOST      (default: 10.50.0.4)
  OBS_PORT      (default: 4455)
  OBS_PASSWORD  (default: read from /home/clio/.openclaw/secrets/obs_pc_ws_pass)
  EXPORTER_PORT (default: 9121)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

import websockets
from prometheus_client import Gauge, Info, Counter, start_http_server

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("obs_exporter")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OBS_HOST = os.environ.get("OBS_HOST", "10.50.0.4")
OBS_PORT = int(os.environ.get("OBS_PORT", "4455"))
EXPORTER_PORT = int(os.environ.get("EXPORTER_PORT", "9121"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "10"))
RECONNECT_DELAY = int(os.environ.get("RECONNECT_DELAY", "5"))
OBS_PASSWORD_FILE = "/home/clio/.openclaw/secrets/obs_pc_ws_pass"


def _read_password() -> str:
    """Read OBS password from env var or file."""
    pw = os.environ.get("OBS_PASSWORD")
    if pw:
        return pw.strip()
    try:
        return Path(OBS_PASSWORD_FILE).read_text().strip()
    except FileNotFoundError:
        log.error("Password file not found: %s", OBS_PASSWORD_FILE)
        sys.exit(1)
    except Exception as e:
        log.error("Failed to read password file: %s", e)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Prometheus Metrics
# ---------------------------------------------------------------------------

# System metrics (gauges — point-in-time values)
obs_cpu_usage = Gauge("obs_cpu_usage", "OBS CPU usage percentage")
obs_memory_usage_mb = Gauge("obs_memory_usage_mb", "OBS memory usage in MB")
obs_active_fps = Gauge("obs_active_fps", "OBS active FPS")
obs_available_disk_space_mb = Gauge(
    "obs_available_disk_space_mb", "Available disk space in MB for recording path"
)
obs_average_frame_render_time_ms = Gauge(
    "obs_average_frame_render_time_ms", "Average frame render time in ms"
)

# Stream metrics
obs_stream_active = Gauge(
    "obs_stream_active", "1 if streaming is active, 0 otherwise"
)
obs_stream_bytes_total = Counter(
    "obs_stream_bytes_total", "Total bytes sent while streaming"
)
obs_stream_duration_seconds = Gauge(
    "obs_stream_duration_seconds", "Current stream duration in seconds"
)
obs_stream_congestion = Gauge("obs_stream_congestion", "Stream congestion (0-1)")
obs_stream_skipped_frames_total = Counter(
    "obs_stream_skipped_frames_total", "Total frames skipped by the stream output"
)
obs_stream_total_frames = Counter(
    "obs_stream_total_frames", "Total frames output by the stream"
)

# Recording metrics
obs_recording_active = Gauge(
    "obs_recording_active", "1 if recording is active, 0 otherwise"
)
obs_recording_bytes_total = Counter(
    "obs_recording_bytes_total", "Total bytes written while recording"
)
obs_recording_duration_seconds = Gauge(
    "obs_recording_duration_seconds", "Current recording duration in seconds"
)
obs_recording_paused = Gauge(
    "obs_recording_paused", "1 if recording is paused, 0 otherwise"
)

# Render metrics
obs_render_skipped_frames_total = Counter(
    "obs_render_skipped_frames_total", "Total frames skipped by the video renderer"
)
obs_render_total_frames = Counter(
    "obs_render_total_frames", "Total frames rendered"
)

# Info label
obs_info = Info("obs", "OBS Studio version and current scene information")


# ---------------------------------------------------------------------------
# Counter tracking helpers
# ---------------------------------------------------------------------------

class CumulativeTracker:
    """Track OBS cumulative counters and feed deltas to prometheus_client Counter.

    OBS reports monotonically increasing totals (stream bytes, frame counts, etc.).
    prometheus_client.Counter expects incremental calls.
    This class stores the last-seen value and increments by the delta.

    If the value goes DOWN (OBS restarted), we note it and re-baseline.
    """

    def __init__(self, prom_counter: Counter):
        self._counter = prom_counter
        self._prev: Optional[float] = None

    def update(self, current: float) -> None:
        if self._prev is None:
            # First observation — set baseline, no increment yet
            self._prev = current
            return

        delta = current - self._prev
        if delta >= 0:
            self._counter.inc(delta)
        else:
            # Counter reset (OBS restarted) — re-baseline
            log.warning(
                "Counter %s went backwards (%.0f -> %.0f), re-baselining",
                self._counter._name,
                self._prev,
                current,
            )
        self._prev = current

    def reset(self) -> None:
        """Called on disconnect to clear state."""
        self._prev = None


# Create tracker instances
_stream_bytes_tracker = CumulativeTracker(obs_stream_bytes_total)
_stream_skipped_tracker = CumulativeTracker(obs_stream_skipped_frames_total)
_stream_frames_tracker = CumulativeTracker(obs_stream_total_frames)
_recording_bytes_tracker = CumulativeTracker(obs_recording_bytes_total)
_render_skipped_tracker = CumulativeTracker(obs_render_skipped_frames_total)
_render_frames_tracker = CumulativeTracker(obs_render_total_frames)

ALL_TRACKERS = [
    _stream_bytes_tracker,
    _stream_skipped_tracker,
    _stream_frames_tracker,
    _recording_bytes_tracker,
    _render_skipped_tracker,
    _render_frames_tracker,
]


def _reset_all_trackers():
    """Reset all counter trackers (on disconnect)."""
    for t in ALL_TRACKERS:
        t.reset()


# ---------------------------------------------------------------------------
# OBS WebSocket v5 Protocol helpers
# ---------------------------------------------------------------------------

# OBS WebSocket v5 op codes
OP_HELLO = 0
OP_IDENTIFY = 1
OP_IDENTIFIED = 2
OP_RECONNECT = 3
OP_EVENT = 5
OP_REQUEST = 6
OP_REQUEST_RESPONSE = 7


def _compute_auth_response(password: str, challenge: str, salt: str) -> str:
    """Compute OBS WebSocket v5 authentication response.

    Algorithm (from obs-websocket docs):
      1. secret = base64( sha256( password + salt ) )
      2. authentication = base64( sha256( secret + challenge ) )
    """
    pass_salt = password + salt
    secret = base64.b64encode(hashlib.sha256(pass_salt.encode()).digest()).decode()
    combined = secret + challenge
    auth_response = base64.b64encode(
        hashlib.sha256(combined.encode()).digest()
    ).decode()
    return auth_response


class OBSExporter:
    """Manages OBS WebSocket connection and metric collection."""

    def __init__(self, password: str):
        self._password = password
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._request_id = 0
        self._pending: dict = {}  # request_id -> Future
        self._authenticated = False

    async def _send(self, op: int, d: dict) -> None:
        """Send a JSON message to OBS WebSocket."""
        if self._ws is None:
            raise ConnectionError("WebSocket not connected")
        msg = json.dumps({"op": op, "d": d})
        await self._ws.send(msg)

    async def request(self, req_type: str, request_data: Optional[dict] = None) -> dict:
        """Send a request to OBS and wait for the response.

        Returns the responseData dict, or empty dict on timeout/error.
        """
        self._request_id += 1
        rid = str(self._request_id)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[rid] = fut

        payload = {"requestType": req_type, "requestId": rid}
        if request_data:
            payload["requestData"] = request_data

        await self._send(OP_REQUEST, payload)

        try:
            result = await asyncio.wait_for(fut, timeout=5.0)
            # result is the full "d" dict from op=7
            req_status = result.get("requestStatus", {})
            if not req_status.get("result", False):
                log.debug(
                    "OBS request %s failed: %s",
                    req_type,
                    req_status.get("comment", "unknown"),
                )
            return result.get("responseData", {})
        except asyncio.TimeoutError:
            log.warning("OBS request %s (id=%s) timed out", req_type, rid)
            self._pending.pop(rid, None)
            return {}

    def _on_message(self, raw: str) -> Optional[asyncio.coroutine]:
        """Dispatch an incoming OBS WebSocket message.
        Returns a coroutine to be awaited if needed, else None.
        """
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Invalid JSON from OBS: %s", raw[:200])
            return None

        op = msg.get("op")
        d = msg.get("d", {})

        if op == OP_HELLO:
            # Server hello — return the identify coroutine
            return self._identify(d)

        elif op == OP_IDENTIFIED:
            self._authenticated = True
            log.info("Authenticated with OBS WebSocket v5")

        elif op == OP_REQUEST_RESPONSE:
            rid = d.get("requestId")
            if rid and rid in self._pending:
                fut = self._pending.pop(rid)
                if not fut.done():
                    fut.set_result(d)

        elif op == OP_RECONNECT:
            log.info("OBS requested reconnect")

        elif op == OP_EVENT:
            pass  # We don't subscribe to events

        return None

    async def _identify(self, hello_d: dict) -> None:
        """Handle Hello message and send Identify with auth."""
        auth_info = hello_d.get("authentication")
        identify = {"rpcVersion": 1, "eventSubscriptions": 0}

        if auth_info:
            challenge = auth_info.get("challenge", "")
            salt = auth_info.get("salt", "")
            auth_response = _compute_auth_response(self._password, challenge, salt)
            identify["authentication"] = auth_response
            log.info(
                "Computed auth response (password len=%d, salt len=%d, challenge len=%d)",
                len(self._password), len(salt), len(challenge),
            )
        else:
            log.info("No auth required, sending Identify")

        await self._send(OP_IDENTIFY, identify)

    async def _recv_loop(self) -> None:
        """Read messages from the WebSocket until disconnected."""
        try:
            async for raw in self._ws:
                coro = self._on_message(raw)
                if coro is not None:
                    # Identify must be sent synchronously within the recv loop
                    # to avoid the message being queued after more recv messages
                    try:
                        await coro
                    except Exception as e:
                        log.error("Error in identify: %s", e)
                        return
        except websockets.ConnectionClosed as e:
            log.warning(
                "OBS WebSocket closed: code=%s reason=%s", e.code, e.reason
            )
        except Exception as e:
            log.error("recv_loop error: %s", e, exc_info=True)
        finally:
            self._authenticated = False

    # -----------------------------------------------------------------------
    # Metric collection
    # -----------------------------------------------------------------------

    async def _collect_system(self) -> None:
        """Collect system metrics via GetStats."""
        data = await self.request("GetStats")
        if not data:
            return

        cpu = data.get("cpuUsage")
        if cpu is not None:
            obs_cpu_usage.set(round(cpu, 2))

        mem = data.get("memoryUsage")
        if mem is not None:
            obs_memory_usage_mb.set(round(mem, 1))

        fps = data.get("activeFps")
        if fps is not None:
            obs_active_fps.set(round(fps, 2))

        disk = data.get("availableDiskSpace")
        if disk is not None:
            obs_available_disk_space_mb.set(round(disk, 0))

        render_time = data.get("averageFrameRenderTime")
        if render_time is not None:
            obs_average_frame_render_time_ms.set(round(render_time, 3))

        # Render frame stats (also from GetStats in v5)
        render_skipped = data.get("renderSkippedFrames")
        if render_skipped is not None:
            _render_skipped_tracker.update(render_skipped)

        render_total = data.get("renderTotalFrames")
        if render_total is not None:
            _render_frames_tracker.update(render_total)

    async def _collect_stream(self) -> None:
        """Collect stream metrics via GetStreamStatus."""
        data = await self.request("GetStreamStatus")
        if not data:
            return

        active = data.get("outputActive", False)
        obs_stream_active.set(1 if active else 0)

        # Duration (OBS reports in milliseconds)
        duration_ms = data.get("outputDuration", 0)
        obs_stream_duration_seconds.set(duration_ms / 1000.0)

        # Congestion
        congestion = data.get("outputCongestion")
        if congestion is not None:
            obs_stream_congestion.set(round(congestion, 4))

        # Cumulative counters
        output_bytes = data.get("outputBytes", 0)
        _stream_bytes_tracker.update(output_bytes)

        skipped = data.get("outputSkippedFrames", 0)
        _stream_skipped_tracker.update(skipped)

        total = data.get("outputTotalFrames", 0)
        _stream_frames_tracker.update(total)

    async def _collect_recording(self) -> None:
        """Collect recording metrics via GetRecordStatus."""
        data = await self.request("GetRecordStatus")
        if not data:
            return

        active = data.get("outputActive", False)
        obs_recording_active.set(1 if active else 0)

        paused = data.get("outputPaused", False)
        obs_recording_paused.set(1 if paused else 0)

        # Duration (OBS reports in milliseconds)
        duration_ms = data.get("outputDuration", 0)
        obs_recording_duration_seconds.set(duration_ms / 1000.0)

        # Cumulative bytes
        output_bytes = data.get("outputBytes", 0)
        _recording_bytes_tracker.update(output_bytes)

    async def _collect_info(self) -> None:
        """Collect version and scene info."""
        version_data = await self.request("GetVersion")
        if not version_data:
            return

        obs_version = version_data.get("obsVersion", "unknown")

        # Get current scene via GetSceneList
        current_scene = ""
        scene_list = await self.request("GetSceneList")
        if scene_list:
            current_scene = scene_list.get("currentProgramSceneName", "")

        obs_info.info({"version": obs_version, "scene": current_scene})

    async def collect_all_metrics(self) -> None:
        """Collect all metrics from OBS."""
        try:
            await self._collect_system()
            await self._collect_stream()
            await self._collect_recording()
            await self._collect_info()
        except Exception as e:
            log.error("Error collecting metrics: %s", e, exc_info=True)

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------

    async def run(self) -> None:
        """Connect to OBS, authenticate, and poll metrics. Auto-reconnect."""
        url = f"ws://{OBS_HOST}:{OBS_PORT}"
        log.info("Starting OBS exporter → %s (metrics on :%d)", url, EXPORTER_PORT)

        while True:
            try:
                log.info("Connecting to OBS at %s ...", url)
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    open_timeout=10,
                ) as ws:
                    self._ws = ws
                    self._authenticated = False
                    log.info("WebSocket connected, waiting for Hello...")

                    # Start receiving in background
                    recv_task = asyncio.create_task(self._recv_loop())

                    # Wait for authentication (recv_loop handles identify)
                    auth_ok = False
                    for _ in range(100):  # 10 seconds max
                        if self._authenticated:
                            auth_ok = True
                            break
                        if recv_task.done():
                            # Connection closed before auth completed
                            break
                        await asyncio.sleep(0.1)

                    if not auth_ok:
                        log.error("Authentication timed out or connection closed")
                        recv_task.cancel()
                        self._ws = None
                        await asyncio.sleep(RECONNECT_DELAY)
                        continue

                    # Main poll loop
                    log.info("Starting metric collection (every %ds)", POLL_INTERVAL)
                    try:
                        while self._authenticated and not recv_task.done():
                            await self.collect_all_metrics()
                            await asyncio.sleep(POLL_INTERVAL)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.error("Poll loop error: %s", e)

                    if not recv_task.done():
                        recv_task.cancel()
                    else:
                        log.info("Recv task ended, reconnecting")

            except asyncio.CancelledError:
                log.info("Shutting down")
                break
            except (ConnectionRefusedError, OSError) as e:
                log.warning(
                    "Cannot reach OBS: %s — retrying in %ds", e, RECONNECT_DELAY
                )
            except websockets.InvalidHandshake as e:
                log.warning(
                    "WebSocket handshake failed: %s — retrying in %ds",
                    e,
                    RECONNECT_DELAY,
                )
            except websockets.ConnectionClosed as e:
                log.warning(
                    "Connection closed: %s — retrying in %ds", e, RECONNECT_DELAY
                )
            except Exception as e:
                log.error(
                    "Unexpected error: %s — retrying in %ds",
                    e,
                    RECONNECT_DELAY,
                    exc_info=True,
                )
            finally:
                self._ws = None
                self._authenticated = False
                self._pending.clear()
                _reset_all_trackers()

            await asyncio.sleep(RECONNECT_DELAY)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    password = _read_password()
    exporter = OBSExporter(password)

    # Start Prometheus HTTP server (runs in a background thread)
    log.info("Starting Prometheus metrics server on :%d", EXPORTER_PORT)
    start_http_server(EXPORTER_PORT)
    log.info("Metrics available at http://0.0.0.0:%d/metrics", EXPORTER_PORT)

    # Create and set event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Graceful shutdown handler
    def _shutdown(signum: int, _frame) -> None:
        sig_name = signal.Signals(signum).name if signum else "unknown"
        log.info("Received signal %s, shutting down...", sig_name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(exporter.run())
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()
        log.info("Goodbye")


if __name__ == "__main__":
    main()
