# INIM Prime (local)

Local, **offline-first** Home Assistant integration for **INIM Prime / PrimeX** alarm panels — talks only to the panel's on-board HTTP API on your LAN. **No cloud account, no rate limits.**

- 🛡️ **Areas** as `alarm_control_panel` (arm away/home/night, disarm)
- 🎬 **Scenarios** as a selector + per-scenario sensors
- 🚪 **Zones** as `binary_sensor` (open/closed) + per-zone bypass switches
- ⚡ **Outputs** as switches · 🔋 supply voltage, faults & diagnostics as sensors
- 🔔 **Realtime, local**: panel HTTP event-push → HA webhook, plus a hardened two-tier **adaptive poll** (idle/active) — no cloud websocket
- 🔑 **Reconfigure flow** to rotate the API key in place

Configure with your panel's IP, port (8080) and the API key generated in PrimeStudio. See the [README](https://github.com/thekoma/inim-prime-local) for setup, polling/tuning and realtime details.

> Community project, **not affiliated with INIM Electronics**.
