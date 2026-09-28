# Polling & tuning

This integration is **local-polling** with an optional realtime push layer ([realtime.md](realtime.md)). This page explains how the polling behaves and how to tune it.

## Two-tier adaptive polling
- **Idle interval** (default `30 s`) — the steady-state cadence.
- **Active interval** (default `1 s`) — used for a short window (~20 s) after any detected change or push event, then it relaxes back to idle.

So most of the time the panel is polled gently, but right after something happens the integration speeds up automatically for responsive follow-up. Both intervals are editable in the integration **Options**.

## What the numbers mean (measured on a real PrimeX 4.07)
| Measurement | Value |
|---|---|
| Single request latency | ~50–100 ms |
| Full refresh cycle (5 reads) | **~0.5 s** |
| Server-side data freshness | **~0.2 s** (effectively live; no slow internal refresh) |
| Sustained burst | 0 failures over 150 back-to-back requests |

Takeaways:
- The data on the panel is **live** — polling faster directly improves responsiveness down to the cycle floor.
- The **cycle time (~0.5 s)**, not the panel, is the practical floor; going below ~0.5 s/cycle gains nothing.
- `~1 s` active polling → changes seen within ~1–1.5 s. `2–3 s` → within a few seconds. Pick what suits you.

## Does fast polling slow Home Assistant?
No. Polling is fully async (the event loop is free during network waits), payloads are a few KB, and the recorder logs only *actual* state changes — not every poll. The load lands on the panel, and the panel handles it comfortably.

## Safety guards (so HA never suffers)
- **Per-request timeout** 5 s (+ 3 s connect) — an unreachable panel fails fast.
- **Per-cycle hard timeout** 20 s — a stuck cycle is aborted, it never hangs.
- **Transient-failure tolerance** — a failed or timed-out cycle keeps the last good snapshot; entities only go *unavailable* after 3 consecutive failed cycles. A single slow cycle therefore no longer flaps every entity to `unavailable` (which made state-change automations miss arm/disarm events).
- **No overlap / no pile-up** — at most one cycle is ever in flight; a refresh that fires mid-cycle reuses the cached snapshot instead of queueing.
- **Failure backoff** — after repeated failures the interval relaxes to idle, so a dead/slow panel is not hammered; it recovers on the next success.
- **Diagnostic reads are throttled** — the API-stats read (`get_status_api`) is refreshed every 10 minutes instead of every cycle.

## When the panel is slow
Not every panel matches the numbers above. One PrimeX in the field answered even `ping` in ~0.4 s (spikes to ~3 s), so a full cycle took ~7 s. If you see *"panel did not respond"* errors:
- keep the idle interval at the 30 s default (or higher) so the panel is not saturated;
- consider push ([realtime.md](realtime.md)) so arm/disarm events do not depend on polling;
- check the panel's network link (cable, switch port, LAN module) — high ICMP latency to the panel points there.

## Recommended profiles
| Goal | Idle | Active | Push |
|---|---|---|---|
| Balanced (default) | 30 s | 1 s | off |
| Snappy, zero setup | 15 s | 1 s | off |
| Instant on key events | 30 s | 2 s | **on** (webhook) |
