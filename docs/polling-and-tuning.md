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
The numbers above are for **back-to-back** requests. The panel appears to cache its state: requests in quick succession are answered fast, but after an idle pause it re-reads its channels and the first few requests become slow. Measured on the same PrimeX:

| Pattern | Per-request latency |
|---|---|
| Back-to-back requests | ~0.25–0.5 s |
| First ~3 requests after a 20 s pause | ~2.5–3 s each |

So a poll cycle that starts after the idle interval pays the re-read cost and can take **~7 s**. This is expected, not a network fault (ICMP to the panel stays at a few ms). The transient-failure tolerance above absorbs the occasional cycle that is even slower. If you still see *"panel did not respond"* errors:
- keep the idle interval at the 30 s default (or higher) so the panel is not saturated;
- use push ([realtime.md](realtime.md)) so arm/disarm events do not depend on polling at all.

## Fast area and zone polling (native protocol)
The arm/disarm state of every area, and the state of every zone (open/closed, excluded, alarm memory), are also read every **2 s** over the panel's native TCP 6004 protocol, using the read-only *partition status* and *terminal status* commands (the same channel PrimeStudio uses; the command layout follows [Pitscheider/inim-prime-native](https://github.com/Pitscheider/inim-prime-native)). Measured on a PrimeX 4.07:

| | Native partition status | cgi `get_partitions_status` |
|---|---|---|
| Back-to-back | ~6–12 ms | ~0.45–0.7 s |
| First read after a 25 s pause | ~15–430 ms | ~2 s |

Zones are read only for the terminals that host a configured zone, in requests of up to 20 terminals; on a PrimeX with 20 zones one full native cycle (areas + zones) takes ~15–50 ms. Zone *n* is half A of terminal *n*, zone *n + 1005* is half B of terminal *n* on double-zone terminals.

When the native read sees a change, entities update immediately. An **area** change also arms a fast cgi poll to reconcile the rest; zone changes do not, so doors opening and closing never keep the cgi in its fast tier. The channel uses one persistent TCP connection (the panel accepts several concurrent 6004 clients, so PrimeStudio can still connect). While the native poll is healthy the cgi poll rests at **5 minutes** instead of the idle interval (areas, zones and scenario state are native; the cgi then only refreshes outputs, faults and diagnostics). A longer idle interval set in the options is kept. After an area change the usual fast cgi window still runs, then relaxes back to 5 minutes.

Each status read has a 5 s ceiling (cold reads after the channel sat idle were measured at 3.4–4 s). If a read is still running when the next 2 s tick fires, that tick is skipped rather than queued behind it, so a slow panel never builds a backlog of native reads.

Native failures never mark entities unavailable: after 3 consecutive failures the native poll backs off to every ~30 s, the cgi poll returns to the idle interval, and availability stays driven by the cgi poll. Disable it with **Fast area and zone polling (native protocol)** in the options.

## Native structure (read once at setup)
Before the first cgi poll, setup reads the panel's static structure over the same native channel (read-only; ~0.7 s on a PrimeX 4.07). The layouts follow [Pitscheider/inim-prime-native](https://github.com/Pitscheider/inim-prime-native):

| Object | Exists when | Label from |
|---|---|---|
| Area | its *configured* bit is set in the partition status | partition label table |
| Zone | its terminal is single-zone (zone *n*) or double-zone (zones *n* and *n + 1005*), and its partition bitmask in the zone settings is non-zero | zone label table |
| Scenario | its label is not the factory default (`Scenario 10`, `SCENARIO   031`, …) | arming-scenario label table |
| Output | panel output terminal 1005–1009 of the *output* type | output label table |

Every cgi cycle still reads everything; the native structure is then applied, while live state keeps coming from the cgi and the native poll:

- an object both report keeps the cgi state, with the native label;
- an area or zone only the native structure lists is added only while **Fast area and zone polling** is on, since that poll is what keeps its state live. Until the first native tick (≤ 2 s) it shows a neutral state (disarmed, closed);
- an area, zone or scenario only the cgi reports is **kept** as the cgi has it, so a security object is never hidden because of the native rules alone;
- any difference between the cgi and native sets is logged as a warning once per kind (please report it);
- outputs come from the native structure only: the cgi output list is known to be wrong. An output the cgi does not report has an unknown on/off state.

The structure read is best effort, in three steps with their own time budget (outside the setup read's): the partition scan (areas), the terminal scan (zones and outputs) and the label read (everything). If a step fails, for example on a panel variant that rejects part of the terminal scan, only the kinds that depend on it fall back to the cgi's list and names, and a warning is logged. Setup never fails or retries because of it.

Entity unique IDs are unchanged, so existing entities keep their history. The structure is not re-read while running: reload the integration after adding, removing or renaming objects on the panel.

## Recommended profiles
| Goal | Idle | Active | Push |
|---|---|---|---|
| Balanced (default) | 30 s | 1 s | off |
| Snappy, zero setup | 15 s | 1 s | off |
| Instant on key events | 30 s | 2 s | **on** (webhook) |
