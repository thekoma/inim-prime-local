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
- **Per-cycle hard timeout** 8 s — a stuck cycle becomes an `UpdateFailed` (entities go *unavailable*), it never hangs.
- **No overlap / no pile-up** — at most one cycle is ever in flight; a refresh that fires mid-cycle reuses the cached snapshot instead of queueing.
- **Failure backoff** — after repeated failures the interval relaxes to idle, so a dead/slow panel is not hammered; it recovers on the next success.

## Recommended profiles
| Goal | Idle | Active | Push |
|---|---|---|---|
| Balanced (default) | 30 s | 1 s | off |
| Snappy, zero setup | 15 s | 1 s | off |
| Instant on key events | 30 s | 2 s | **on** (webhook) |
