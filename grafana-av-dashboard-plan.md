# EC Grafana A/V Dashboard — Plan

## Constraints
- **READ ONLY** — no commands, no config changes, no state mutations on any hardware
- Only scrape/query existing data
- Primary dashboard assumes hardware is ON and running (live service context)

## Primary Dashboard — Critical Live Service Stats
1. **OBS** — CPU usage, memory, recording status, recording duration, current scene, dropped frames
2. **Network I/O** — Media A/V device throughput, switch port stats, bandwidth utilization
3. **Streaming/Recording** — Current bitrate, bandwidth, recording time, output resolution
4. **Device Health** — Companion module connections, device online/offline status

## Secondary Panels (nice-to-have during service)
- Shure QLXD RF levels, battery status
- ATEM input/output status
- WING mixer levels/connection
- Projector status

## Data Sources
| Source | Endpoint | Protocol | Notes |
|--------|----------|----------|-------|
| Bitfocus Companion | 10.50.0.10:8000 | HTTP REST | Module vars, button states, feedback |
| OBS | TBD (likely on Emanuels-Mini or media server) | OBS WebSocket | CPU, recording, bitrate, scenes |
| Unifi Gateway | 10.50.0.1 | HTTPS REST | Client stats, network I/O per device |
| Shure QLXD | 10.50.0.171-178 | HTTP | RF levels, battery (web UI scraping) |
| ATEM | 10.50.0.6 | via Companion | Input status, program/preview |
| WING | 10.50.0.100 | via Companion | Connection status, levels |
| NETGEAR Switch | 10.50.1.1 | HTTP/SNMP | Port stats, throughput |
| Restreamer | 10.70.0.141 (Swarm) | HTTP | Stream status, viewer count |

## Implementation Phases
1. **Discovery** — Probe each API for available read-only data
2. **Exporters** — Build Prometheus exporters/scrapers for each source
3. **Dashboard** — Assemble Grafana JSON with panels
4. **Alerts** — Battery low, device offline, stream issues

## Files
- `av-api-discovery.md` — API discovery results
- `grafana-av-dashboard-plan.md` — this file
