#!/usr/bin/env python3
"""
Prometheus exporter for Shure QLXD wireless receiver metrics.

Connects to 8 Shure QLXD receivers via TCP port 2202, sets METER_RATE to 250ms
for real-time SAMPLE data, and exposes RF level, audio level, antenna status,
device info, transmitter state, and audio gain as Prometheus metrics on port 9122.

Protocol reference:
  - GET:  < GET <ch> <cmd> >          → < REP <ch> <cmd> <value> >
  - SET:  < SET <ch> METER_RATE 00250 >
  - SAMPLE: < SAMPLE <ch> ALL <ant> <rf> <audio> >

Usage:
  python shure_exporter.py
  LOG_LEVEL=DEBUG python shure_exporter.py
"""

import os
import socket
import threading
import time
import logging
import signal
import sys
from prometheus_client import start_http_server, Gauge, Info

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RECEIVERS = {
    "QLXD-1": "10.50.0.171",
    "QLXD-2": "10.50.0.172",
    "QLXD-3": "10.50.0.173",
    "QLXD-4": "10.50.0.174",
    "QLXD-5": "10.50.0.175",
    "QLXD-6": "10.50.0.176",
    "QLXD-7": "10.50.0.177",
    "QLXD-8": "10.50.0.178",
}

TCP_PORT = int(os.environ.get("SHURE_TCP_PORT", "2202"))
METRICS_PORT = int(os.environ.get("SHURE_METRICS_PORT", "9122"))
METER_RATE = os.environ.get("SHURE_METER_RATE", "00250")  # 250 ms
RECONNECT_BASE = 5   # initial backoff seconds
RECONNECT_MAX = 60   # max backoff seconds
CHANNELS = [1, 2, 3, 4]  # QLXD supports up to 4 channels
CONFIG_CMDS = ["DEVICE_ID", "FW_VER", "FREQUENCY", "AUDIO_GAIN", "TX_TYPE", "RF_ANTENNA"]
CONFIG_REFRESH_INTERVAL = 300  # re-query config every 5 minutes
READ_TIMEOUT = 30  # seconds – if no data for 30 s, treat as stale

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("shure_exporter")

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

RF_LEVEL = Gauge(
    "shure_rf_level",
    "RF signal level (0-110, higher is better)",
    ["receiver", "channel", "frequency"],
)

RF_ANTENNA = Gauge(
    "shure_rf_antenna",
    "Currently active RF antenna (1 = active)",
    ["receiver", "antenna"],
)

RF_ANTENNA_ACTIVE = Gauge(
    "shure_rf_antenna_active",
    "Per-antenna active state (1=active, 0=inactive)",
    ["receiver", "antenna"],
)

AUDIO_LEVEL = Gauge(
    "shure_audio_level",
    "Audio input level (0-127)",
    ["receiver", "channel"],
)

DEVICE_INFO = Info(
    "shure_device",
    "Shure receiver device information",
    ["receiver"],
)

TRANSMITTER_ACTIVE = Gauge(
    "shure_transmitter_active",
    "Whether a transmitter is active (0 if TX_TYPE is UNKN)",
    ["receiver"],
)

AUDIO_GAIN = Gauge(
    "shure_audio_gain",
    "Audio gain setting in dB",
    ["receiver"],
)

CONNECTED = Gauge(
    "shure_connected",
    "Whether the exporter is connected to the receiver (1=connected)",
    ["receiver"],
)


# ---------------------------------------------------------------------------
# ShureReceiver – one per physical receiver
# ---------------------------------------------------------------------------

class ShureReceiver:
    """Manages a persistent TCP connection to a single Shure QLXD receiver."""

    def __init__(self, name: str, host: str, port: int = TCP_PORT):
        self.name = name
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self.running = True

        # Per-channel state
        self.frequency: dict[str, str] = {}
        self.tx_type: dict[str, str] = {}
        self.audio_gain_val: dict[str, int] = {}

        # Device-level state
        self.firmware: str = ""
        self.device_id: str = ""
        self.rf_antenna: str = ""

    # ---- connection lifecycle ----

    def _connect(self) -> bool:
        """Open TCP socket and perform initial handshake."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((self.host, self.port))
            sock.settimeout(READ_TIMEOUT)
            self.sock = sock
            self.connected = True
            CONNECTED.labels(receiver=self.name).set(1)
            log.info("[%s] Connected to %s:%d", self.name, self.host, self.port)

            # Set meter rate on every channel
            for ch in CHANNELS:
                self._send(f"< SET {ch} METER_RATE {METER_RATE} >")

            # Query static / config data
            self._query_config()
            return True

        except (socket.error, OSError) as exc:
            log.warning("[%s] Connection failed: %s", self.name, exc)
            self._mark_disconnected()
            return False

    def _disconnect(self):
        """Close socket and update state."""
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self._mark_disconnected()

    def _mark_disconnected(self):
        self.connected = False
        CONNECTED.labels(receiver=self.name).set(0)

    # ---- send / receive ----

    def _send(self, msg: str) -> bool:
        """Send a newline-terminated message. Returns False on failure."""
        if not self.sock:
            return False
        try:
            with self._send_lock:
                self.sock.sendall((msg + "\n").encode("ascii", errors="ignore"))
            return True
        except (socket.error, OSError) as exc:
            log.warning("[%s] Send failed: %s", self.name, exc)
            self._disconnect()
            return False

    def _query_config(self):
        """Send GET commands for all config fields on every channel."""
        for ch in CHANNELS:
            for cmd in CONFIG_CMDS:
                if not self._send(f"< GET {ch} {cmd} >"):
                    return
                time.sleep(0.05)

    # ---- parsing ----

    def _parse(self, line: str):
        """Parse a single Shure protocol message (already stripped of < >)."""
        parts = line.split()
        if not parts:
            return

        msg_type = parts[0]

        # --- SAMPLE <ch> ALL <antenna> <rf_level> <audio_level> ---
        if msg_type == "SAMPLE" and len(parts) >= 6:
            channel = parts[1]
            antenna = parts[3]
            try:
                rf_level = int(parts[4])
                audio_level = int(parts[5])
            except ValueError:
                return

            freq = self.frequency.get(channel, "0")
            RF_LEVEL.labels(
                receiver=self.name, channel=channel, frequency=freq
            ).set(rf_level)
            AUDIO_LEVEL.labels(
                receiver=self.name, channel=channel
            ).set(audio_level)

            # Update antenna states from SAMPLE data
            for ant in ("AX", "BX"):
                active = 1 if ant == antenna else 0
                RF_ANTENNA_ACTIVE.labels(receiver=self.name, antenna=ant).set(active)
                RF_ANTENNA.labels(receiver=self.name, antenna=ant).set(active)

        # --- REP <ch> <cmd> <value...> ---
        elif msg_type == "REP" and len(parts) >= 4:
            channel = parts[1]
            command = parts[2]
            value = " ".join(parts[3:])
            self._handle_rep(channel, command, value)

    def _handle_rep(self, channel: str, command: str, value: str):
        """Process a REP response and update metrics."""
        if command == "FREQUENCY":
            self.frequency[channel] = value
            log.debug("[%s] Ch%s FREQUENCY=%s", self.name, channel, value)

        elif command == "FW_VER":
            self.firmware = value
            log.debug("[%s] FW_VER=%s", self.name, value)
            self._update_device_info()

        elif command == "DEVICE_ID":
            self.device_id = value
            log.debug("[%s] DEVICE_ID=%s", self.name, value)
            self._update_device_info()

        elif command == "TX_TYPE":
            self.tx_type[channel] = value
            is_active = 0 if value.strip().upper() == "UNKN" else 1
            TRANSMITTER_ACTIVE.labels(receiver=self.name).set(is_active)
            log.debug("[%s] Ch%s TX_TYPE=%s (active=%d)", self.name, channel, value, is_active)
            self._update_device_info()

        elif command == "AUDIO_GAIN":
            try:
                gain = int(value)
                self.audio_gain_val[channel] = gain
                AUDIO_GAIN.labels(receiver=self.name).set(gain)
            except ValueError:
                pass
            log.debug("[%s] Ch%s AUDIO_GAIN=%s", self.name, channel, value)

        elif command == "RF_ANTENNA":
            self.rf_antenna = value.strip()
            for ant in ("AX", "BX"):
                RF_ANTENNA.labels(receiver=self.name, antenna=ant).set(
                    1 if ant == self.rf_antenna else 0
                )
            log.debug("[%s] RF_ANTENNA=%s", self.name, value)

    def _update_device_info(self):
        """Push the shure_device Info metric."""
        tx = next(iter(self.tx_type.values()), "unknown")
        DEVICE_INFO.labels(receiver=self.name).info({
            "firmware": self.firmware,
            "device_id": self.device_id,
            "tx_type": tx,
        })

    # ---- read loop ----

    def _read_loop(self):
        """Block reading from socket, parse complete messages."""
        buf = ""
        while self.running and self.connected:
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                # No data within READ_TIMEOUT – send a keepalive GET
                self._send(f"< GET 1 DEVICE_ID >")
                continue
            except (socket.error, OSError) as exc:
                log.warning("[%s] Read error: %s", self.name, exc)
                self._disconnect()
                return

            if not data:
                log.warning("[%s] Connection closed by remote", self.name)
                self._disconnect()
                return

            buf += data.decode("ascii", errors="ignore")

            # Process all complete messages (delimited by >)
            while ">" in buf:
                idx = buf.index(">")
                raw = buf[:idx].strip()
                buf = buf[idx + 1:]
                # Strip leading < if present
                if raw.startswith("<"):
                    raw = raw[1:].strip()
                if raw:
                    self._parse(raw)

    # ---- periodic config refresh ----

    def _config_refresh_loop(self):
        """Periodically re-query config to catch changes."""
        while self.running:
            time.sleep(CONFIG_REFRESH_INTERVAL)
            if self.connected:
                log.debug("[%s] Refreshing config…", self.name)
                self._query_config()

    # ---- main entry point ----

    def run(self):
        """Connect, read, reconnect with exponential backoff."""
        backoff = RECONNECT_BASE
        while self.running:
            if self._connect():
                backoff = RECONNECT_BASE
                # Start config refresh in background
                refresh_thread = threading.Thread(
                    target=self._config_refresh_loop,
                    name=f"cfg-{self.name}",
                    daemon=True,
                )
                refresh_thread.start()
                # Block on read until disconnect
                self._read_loop()
            else:
                log.info("[%s] Retrying in %ds…", self.name, backoff)
                # Interruptible sleep
                deadline = time.monotonic() + backoff
                while self.running and time.monotonic() < deadline:
                    time.sleep(0.5)
                backoff = min(backoff * 2, RECONNECT_MAX)

    def stop(self):
        self.running = False
        self._disconnect()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("Starting Shure QLXD exporter on :%d", METRICS_PORT)
    start_http_server(METRICS_PORT)
    log.info("Metrics available at http://0.0.0.0:%d/metrics", METRICS_PORT)

    receivers: list[ShureReceiver] = []
    threads: list[threading.Thread] = []

    for name, host in RECEIVERS.items():
        rx = ShureReceiver(name, host)
        receivers.append(rx)
        t = threading.Thread(target=rx.run, name=f"shure-{name}", daemon=True)
        threads.append(t)
        t.start()
        log.info("Spawned thread for %s (%s)", name, host)

    # Graceful shutdown on SIGTERM/SIGINT
    def _shutdown(signum, frame):
        log.info("Received signal %d – shutting down…", signum)
        for rx in receivers:
            rx.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt – shutting down…")
        for rx in receivers:
            rx.stop()


if __name__ == "__main__":
    main()
