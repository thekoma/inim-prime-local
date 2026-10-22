# Polling & tuning

This integration is **local-polling** over the panel's native TCP 6004 protocol, with an optional realtime push layer ([realtime.md](realtime.md)). There are no polling options: this page explains what is read, when, and how failures are handled.

## What is read every 2 seconds
One cycle sends four read-only status commands, one after the other, over a single persistent TCP connection (the layouts follow [Pitscheider/inim-prime-native](https://github.com/Pitscheider/inim-prime-native) and Prime/STUDIO's own monitoring page):

| Command | Gives |
|---|---|
| Partition status (op 6) | every area's arming mode, alarm and sabotage state, alarm memory |
| Terminal status (op 7), zone terminals | every zone's state (ready, open, tamper, short), bypass, alarm memory |
| Terminal status (op 7), output terminals | every output's on/off state |
| System status (op 0x0D) | the panel faults (mains, battery, phone line, GSM, sirens, wireless…), the tampers and which device reports them, the service jumper, an open programming session, VoLTE, and the supply and battery voltages |

Measured on a PrimeX 4.07, each command answers in about **10 ms** (a first read on a connection that sat idle was measured at up to ~4 s, hence the 5 s ceiling below). Zones are read only for the terminals that host a configured zone, in requests of up to 20 terminals. A change on the panel therefore shows up in Home Assistant within about 2 s, without any push setup.

Two slower-changing readings ride on the same cycle, less often:

| Command | Every | Gives |
|---|---|---|
| Power values (op 0x31) | 1 minute | the battery temperature and internal resistance |
| GSM status (op 0x5E) | 5 minutes | the LTE communicator's signal level (0–100), networks (2G/3G/4G), operator code (MCC+MNC) and faults. The panel asks the modem for these, hence the longer interval |

The GSM reply also carries the SIM and modem identifiers (IMSI, ICCID, IMEI): the integration decodes only the status bytes and the operator code, and never keeps, logs or shows the identifiers. A panel without a GSM module gets no GSM sensor. If either reading fails, the last one is kept and the cycle goes on: they are not needed for the alarm itself.

The panel accepts several concurrent 6004 clients, so Prime/STUDIO can still connect while Home Assistant is running.

## Does it slow Home Assistant or the panel?
No. The reads are async (the event loop is free while waiting), the answers are a few hundred bytes, and the recorder logs only *actual* state changes, not every poll. The panel answers these status commands from memory; Prime/STUDIO's monitoring page polls the same commands.

## Safety guards
- **Per-read timeout** 5 s — an unreachable panel fails fast, and the connection is closed and reopened on the next read.
- **Per-cycle hard timeout** 20 s — a stuck cycle is aborted, it never hangs.
- **Transient-failure tolerance** — a failed or timed-out cycle keeps the last good state; entities only go *unavailable* after 3 consecutive failed cycles. A single slow answer does not flap every entity to `unavailable`.
- **Failure backoff** — after 3 failed cycles in a row the integration reads every 30 s instead of every 2 s, so a panel that stopped answering is not hammered; the 2 s cadence resumes on the next success.
- **No overlap / no pile-up** — at most one cycle is ever in flight; a refresh that fires mid-cycle reuses the current state instead of queueing.
- **Commands share the lock** — a command never interleaves with a status read on the connection (see [native-commands.md](native-commands.md)), and the state is re-read right after it.

## Structure (read once at setup)
Before the first cycle, setup reads the panel's static structure over the same channel (read-only; under a second on a PrimeX 4.07). The memory map is specific to each firmware family (4.x and 5.x are mapped):

| Object | Exists when | Label from |
|---|---|---|
| Area | its *configured* bit is set in the partition status | partition label table |
| Zone | its terminal is single-zone (zone *n*) or double-zone (zones *n* and *n + 1005*, *n + 1505* on 5.x), and its partition bitmask in the zone settings is non-zero | zone label table |
| Scenario | its label is not the factory default (`Scenario 10`, `SCENARIO   031`, …) | arming-scenario label table |
| Output | a panel output terminal (1005–1009, 1505–1509 on 5.x) of the *output* type | output label table |

The same read gives each zone's programming (type, *Interna*, *Autoescludibile*, *Non escludibile*, fire, the detector type), used for the device class, the bypass switches and the arming check, and each scenario's target mode per area.

The structure read is in three steps with their own time budget: the partition scan (areas), the terminal scan (zones and outputs) and the label and settings read. Setup is retried later if the areas or the zones could not be read; if only the scenarios or the outputs could not be read, setup goes on without them and logs a warning.

The structure is not re-read while running: reload the integration after adding, removing or renaming objects on the panel.
