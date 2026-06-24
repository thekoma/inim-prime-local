# Realtime notifications (fully local)

Polling alone already gives near-realtime updates (~1 s in the active window — see [polling-and-tuning.md](polling-and-tuning.md)). For **instant** updates you can have the panel *push* events to Home Assistant, with **no cloud** involved. Two local paths are available.

## Option A — Panel HTTP event-push → HA webhook (built in)

The panel can fire an HTTP request on each event (the *"Invio pagine web" / outgoing-HTTP* table in PrimeStudio). This integration exposes a Home Assistant webhook that receives those requests and updates the matching entity instantly (optimistically), with the regular poll staying on as a reconciliation backstop.

### 1. Enable push in the integration
**Settings → Devices & Services → INIM Prime → Configure → enable "realtime push"**. After saving, Home Assistant raises a notification with your **webhook URL**, e.g.:

```
http://<home-assistant-ip>:8123/api/webhook/<generated-secret>
```

The generated id *is* the shared secret — keep it private; it works on the LAN only.

### 2. Create the event-actions in PrimeStudio
The panel sends the URL/body **verbatim** — there are no macros and no "all objects" wildcard, so you create **one entry per (object × event × activation/restoral)** and encode the identity in the URL query string. Point each entry (No authentication, GET) at the webhook URL plus parameters:

| Panel event | URL to call | Effect in HA |
|---|---|---|
| Zone opened | `…/api/webhook/<secret>?ev=zone_open&id=<zone>` | zone binary_sensor → on |
| Zone restored | `…?ev=zone_close&id=<zone>` | zone binary_sensor → off |
| Area armed | `…?ev=arm&area=<area>` | alarm panel → armed |
| Area disarmed | `…?ev=disarm&area=<area>` | alarm panel → disarmed |
| Alarm | `…?ev=alarm&area=<area>` | alarm panel → triggered |
| Tamper | `…?ev=tamper` | system fault → on |
| Fault | `…?ev=fault&code=<n>` | system fault → on |
| Output change | `…?ev=output&id=<n>&state=<0\|1>` | output switch |

You can also send the parameters as a form **body** (POST). The handler always returns HTTP `200` and ignores unknown/invalid calls without disturbing state.

### 3. Spend the ~50 event-action slots wisely
Panels have a limited number of slots (e.g. Prime120 ≈ 50). Prioritise: **arm/disarm + alarm + tamper + faults + perimeter zones** on push; leave interior zones and diagnostics on the (already fast) adaptive poll.

> **Why not the INIM cloud websocket?** It works, but it routes your alarm's realtime path through INIM's cloud. This integration is local-first by design and does not use it.

## Option B — SIA-IP (alternative, standards-based)

INIM panels can report events using the **SIA-DC09** central-station protocol. Home Assistant has a built-in [`sia`](https://www.home-assistant.io/integrations/sia/) integration that listens for these reports — a second, fully-local, sub-second realtime path that works independently of this integration. Configure the panel's SIA-IP reporting to point at Home Assistant's SIA listener. Use this if the HTTP event-push table is inconvenient or already in use.

## Fallback — adaptive polling

If you configure no push at all, the integration still tracks changes through its two-tier adaptive poll (idle/active). It's the zero-setup option and remains the reconciliation layer even when push is enabled.
