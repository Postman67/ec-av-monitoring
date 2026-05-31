# Emanuel Church A/V Hardware API Discovery

**Date:** 2026-05-31  
**Purpose:** Document available APIs/protocols for Grafana monitoring dashboard  
**Network:** 10.50.0.x (A/V VLAN), 10.50.1.x (management)

---

## 1. Bitfocus Companion (`10.50.0.10:8000`)

**Status:** ✅ Reachable, REST API available  
**Protocol:** HTTP REST + WebSocket (socket.io)  
**Port:** 8000 (HTTP/WS)  
**Auth:** None required for REST API

### REST API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/connections` | GET | **Full module connection list** with status — PRIMARY data source |
| `/api/location/{page}/{row}/{col}/style` | GET | Button style/state (returned `ok` but no visible data) |
| `/api/location/{page}/{row}/{col}/press` | POST | Trigger button press |

### WebSocket (socket.io)

- Path: `/socket.io/?EIO=4&transport=websocket`
- **Not accessible** from this host (connection refused at handshake — likely needs browser origin or is restricted to localhost/Companion's own subnet)
- The frontend app uses WebSocket for real-time updates (button states, variable changes, feedback)

### Connected Modules (from `/api/connections`)

| Label | Module | Enabled | Status |
|---|---|---|---|
| PC_obs | obs-studio | ✅ | ok |
| Laptop_OBS | obs-studio | ❌ | disabled |
| onyx | obsidiancontrol-onyx | ❌ | disabled |
| OLD-PP | renewedvision-propresenter | ✅ | **ERROR: 404** |
| **ATEM** | bmd-atem | ✅ | ok |
| **WING** | behringer-wing | ✅ | Warning: Loading status |
| YT | youtube-live | ❌ | disabled |
| VOX1-VOX8 | shure-wireless (×8) | ✅ | ok (all 8) |
| unifi | ubiquiti-unifi | ❌ | disabled |
| ECHA | homeassistant-server | ❌ | disabled |
| IEM-X32 | behringer-x32 | ❌ | disabled |
| services-live | planningcenter-serviceslive | ❌ | disabled |
| Qlab_OSC | generic-osc | ❌ | disabled |
| **HD8** | bmd-atem | ✅ | ok |
| PJ_L | generic-tcp-udp | ✅ | ok |
| PJ_R | generic-tcp-udp | ✅ | ok |
| PP7-API | renewedvision-propresenter-api | ✅ | ok |

### Grafana Scraping Strategy

- **Poll `/api/connections`** every 30-60s for module health status
- Parse `status.category` (good/warning/error) and `status.message` for alerting
- Count enabled/disabled/error modules as metrics
- The connection status gives a high-level A/V system health overview

---

## 2. ATEM / Blackmagic (`10.50.0.6`)

**Status:** ⚠️ Reachable (ping OK), but **no HTTP/web interface**  
**Protocol:** Blackmagic ATEM Protocol (proprietary UDP, port 9990)  
**Ports scanned:** 80, 443, 8080, 9990-9993, 3621-3626, 4712-4716 — all closed

### Notes

- ATEM Mini/TVS devices do **not** expose HTTP APIs
- The ATEM is managed entirely through Companion's `bmd-atem` module (connection `fkyDvsigKpheX2ImBbILO`)
- Companion's ATEM module reports `status: ok` — meaning it's connected and communicating
- A second ATEM device ("HD8", connection `xyvmyTIzpR9Qsr-808lx4`) is also connected and ok

### Grafana Scraping Strategy

- Monitor ATEM status via **Companion's `/api/connections`** endpoint
- No direct ATEM API access needed
- For deeper ATEM data (input switching, audio levels), would need the `bmd-atem` Node.js library or Companion's WebSocket for variable data

---

## 3. Shure QLXD Receivers (`10.50.0.171-178`)

**Status:** ✅ All 8 reachable, TCP control protocol working  
**Protocol:** Shure Wireless Control Protocol (TCP text commands)  
**Port:** 2202 (TCP)  
**Auth:** None  
**Firmware:** All running v2.6.2.0

### Protocol Details

- **Text-based command/response** format
- Commands: `< GET <channel> <command> >`
- Responses: `< REP <channel> <command> <value> >`
- Real-time data: `< SAMPLE <channel> ALL <antenna> <rf_level> <audio_level> >`
- Set meter rate: `< SET <channel> METER_RATE <ms> >` (e.g., `00250` = 250ms)

### Available Commands

| Command | Description | Example Response |
|---|---|---|
| `GET DEVICE_ID` | Device identifier | `QLXD1` through `QLXD8` |
| `GET FW_VER` | Firmware version | `2.6.2.0` |
| `GET 1 FREQUENCY` | Operating frequency (Hz/1000) | `501125` = 501.125 MHz |
| `GET 1 AUDIO_GAIN` | Audio gain (0-60 dB scale) | `034` |
| `GET 1 TX_TYPE` | Transmitter type | `QLXD2` or `UNKN` |
| `GET 1 RF_ANTENNA` | Active RF antenna | `AX`, `BX`, `XB`, `XX` |
| `GET 1 METER_RATE` | Sample rate for SAMPLE data | `01000` = 1000ms |
| `SET 1 METER_RATE <ms>` | Set metering rate | `00250` = 250ms |

### Real-time SAMPLE Data

- Format: `SAMPLE 1 ALL <antenna> <rf_level> <audio_level>`
- RF level: 0-110 scale (signal strength)
- Audio level: 0-127 scale (audio meter)
- Antenna: `AX`/`BX`/`XB`/`XX` (antenna diversity status)

### Current Receiver Snapshot

| Receiver | IP | Frequency | TX Type | Gain | Antenna |
|---|---|---|---|---|---|
| QLXD-1 | 10.50.0.171 | 501.125 MHz | QLXD2 | 34 | AX |
| QLXD-2 | 10.50.0.172 | 512.500 MHz | UNKN | 18 | XX |
| QLXD-3 | 10.50.0.173 | 514.900 MHz | UNKN | 18 | XX |
| QLXD-4 | 10.50.0.174 | 522.125 MHz | QLXD2 | 18 | AX |
| QLXD-5 | 10.50.0.175 | 523.575 MHz | QLXD2 | 18 | AX |
| QLXD-6 | 10.50.0.176 | 524.375 MHz | QLXD2 | 18 | AX |
| QLXD-7 | 10.50.0.177 | 527.325 MHz | QLXD2 | 18 | AX |
| QLXD-8 | 10.50.0.178 | 529.700 MHz | QLXD2 | 18 | XB |

**Notes:**
- QLXD-2 and QLXD-3 show `TX_TYPE: UNKN` and `RF_ANTENNA: XX` — transmitters may be off or disconnected
- QLXD-1 has higher gain (34 vs 18) — may be a different mic/channel setup

### Grafana Scraping Strategy

- **Write a TCP scraper** that connects to each receiver on port 2202
- Set `METER_RATE` to `00250` (250ms) for frequent updates
- Parse `SAMPLE` lines for real-time RF and audio levels
- Parse `REP` lines for configuration state
- Alert on: `TX_TYPE: UNKN` (transmitter offline), low RF levels, `RF_ANTENNA: XX` (no signal)
- Also monitor via **Companion's `/api/connections`** — all 8 Shure modules report `ok`

---

## 4. Allen & Heath WING (`10.50.0.100`)

**Status:** ⚠️ Reachable (ping OK), limited access  
**Protocol:** Custom TCP protocol (port 2222)  
**Port:** 2222 (TCP) — HTTP-like, responds with `"Hey, it's me, your WING"`  
**Auth:** Unknown  
**Other ports scanned:** 80, 443, 8080, 8443, 8000, 9000, 51325-51327, 2222, 5000, 3000, 4000, 6000, 7000, 10024, 10025 — all closed except 2222

### Protocol Details

- Port 2222 responds to HTTP GET with text: `"Hey, it's me, your WING"`
- Does **not** accept commands via HTTP or plain TCP text
- Allen & Heath WING typically uses:
  - **OSC (UDP 10024)** for remote control — port appears closed
  - **TCP remote protocol** on port 2222 — needs specific binary/text handshake
  - **WING CoPilot** app uses a proprietary protocol

### Companion Integration

- Companion has `behringer-wing` module enabled (connection `qqvaLb5YjHB5KBiwKoDnH`)
- Status: **Warning: "Loading status"** — may be in initial connection phase or having issues
- Companion's WING module communicates directly with the mixer

### Grafana Scraping Strategy

- Monitor WING status via **Companion's `/api/connections`**
- Direct WING access requires reverse-engineering the TCP protocol or using the `behringer-wing` Companion module
- May need to investigate Companion's WebSocket for WING variable data
- Consider using the **WING's OSC interface** if UDP 10024 can be opened

---

## 5. NETGEAR GS752TP (`10.50.1.1`)

**Status:** ✅ Reachable, web UI + SNMP available  
**Protocol:** HTTP (web UI) + SNMP v2c  
**Ports:** 80 (HTTP, redirects to `/cs9ef215ce/`), 161 (SNMP)  
**Auth:** Web UI requires admin login; SNMP uses `public` community string

### Web Interface

- URL: `http://10.50.1.1/cs9ef215ce/`
- Login form: username + password (default: `admin`)
- Classic Netgear managed switch web UI
- Would need credentials for HTTP scraping

### SNMP Data (verified working)

| OID | Description | Value |
|---|---|---|
| `1.3.6.1.2.1.1.1.0` | sysDescr | 48-Port Gigabit Smart Switch with PoE and 4 SFP uplinks |
| `1.3.6.1.2.1.1.5.0` | sysName | (empty) |
| `1.3.6.1.2.1.1.3.0` | sysUpTime | ~740200 centiseconds (2.05 hours) |
| `1.3.6.1.2.1.2.1.0` | ifNumber | 80 interfaces |

### Port Status (SNMP)

**Active ports (ifOperStatus = up):** 1, 2, 3, 4, 5, 6, 11, 12, 13, 14

**Port speeds (ifSpeed):**
- Ports 1-6, 11, 14: 100 Mbps (100000000)
- Ports 7-10, 12, 13, 15+: 1 Gbps (1000000000)

**Traffic (ifHCInOctets):**
- Ports 1-6: ~1.7 MB each (low traffic)
- Port 12: 429 MB (high traffic — likely uplink or server)
- Port 13: 3.4 MB
- Port 14: 5.4 MB

### Grafana Scraping Strategy

- **Use SNMP exporter** (Prometheus snmp_exporter) for port stats
- Key OIDs to scrape:
  - `ifOperStatus` (1.3.6.1.2.1.2.2.1.8) — port up/down
  - `ifSpeed` (1.3.6.1.2.1.2.2.1.5) — port speed
  - `ifHCInOctets` (1.3.6.1.2.1.31.1.1.1.6) — ingress traffic
  - `ifHCOutOctets` (1.3.6.1.2.1.31.1.1.1.10) — egress traffic
  - `ifInErrors` (1.3.6.1.2.1.2.2.1.14) — input errors
  - `ifOutErrors` (1.3.6.1.2.1.2.2.1.20) — output errors
  - PoE OIDs (if supported by this model)
- Community string: `public` (read-only)
- No auth needed for SNMP v2c with public community

---

## Summary: Grafana Integration Priority

| Device | Direct API | Via Companion | Priority |
|---|---|---|---|
| **Companion** | REST `/api/connections` | — | 🟢 HIGH — central health dashboard |
| **Shure QLXD ×8** | TCP port 2202 | ✅ via `shure-wireless` module | 🟢 HIGH — real-time RF/audio levels |
| **Netgear Switch** | SNMP v2c | ❌ | 🟡 MEDIUM — port stats/traffic |
| **ATEM** | ❌ (no HTTP) | ✅ via `bmd-atem` module | 🟡 MEDIUM — via Companion |
| **WING** | ⚠️ (port 2222 only) | ✅ via `behringer-wing` module | 🟡 MEDIUM — via Companion |

### Recommended Architecture

```
Grafana ← Prometheus ← SNMP Exporter (Netgear)
                     ← Custom Shure Scraper (TCP 2202 × 8)
                     ← Companion Exporter (HTTP /api/connections)
```

### Key Ports Reference

| Device | IP | Port | Protocol |
|---|---|---|---|
| Companion | 10.50.0.10 | 8000 | HTTP REST |
| ATEM | 10.50.0.6 | 9990 | UDP (proprietary) |
| QLXD-1 | 10.50.0.171 | 2202 | TCP (Shure protocol) |
| QLXD-2 | 10.50.0.172 | 2202 | TCP |
| QLXD-3 | 10.50.0.173 | 2202 | TCP |
| QLXD-4 | 10.50.0.174 | 2202 | TCP |
| QLXD-5 | 10.50.0.175 | 2202 | TCP |
| QLXD-6 | 10.50.0.176 | 2202 | TCP |
| QLXD-7 | 10.50.0.177 | 2202 | TCP |
| QLXD-8 | 10.50.0.178 | 2202 | TCP |
| WING | 10.50.0.100 | 2222 | TCP (limited) |
| Netgear | 10.50.1.1 | 161 | SNMP v2c |
| Netgear | 10.50.1.1 | 80 | HTTP (auth required) |
