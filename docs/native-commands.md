# Native commands (experimental)

By default every write (arm/disarm, scenarios, zone bypass, outputs, alarm-memory reset) goes through the panel's cgi, as it always has. The option **Native commands (arm/disarm, bypass, outputs) — experimental** sends them over the panel's native TCP 6004 protocol instead, the same channel the fast area and zone polling already uses.

> **Off by default, not yet live-verified.** The command layouts follow [Pitscheider/inim-prime-native](https://github.com/Pitscheider/inim-prime-native) (GPL-3.0), but this project has not yet run them against a live panel. Turn the option on only to test it as described below, while you are at home.

## What goes native

| Action | Native command | Falls back to the cgi when |
|---|---|---|
| Disarm an area | op 3 (set arming status) | the native command certainly was not sent |
| Arm an area (away / home / night) | op 3 | not sent, **or** the live zone check right before sending finds a zone of the area neither ready nor bypassed (see below) |
| Apply a scenario (button) | op 3, all target areas in one command | not sent, the scenario has a target without a single mode, a target area is unknown, the panel has areas beyond 0–11, or a zone is not ready |
| Zone bypass switch | op 9 (set zone bypass) | not sent |
| Output switch | op 8 (set output) | not sent |
| Clear alarm memory button | op 16 (reset partitions) | not sent |
| `inim_prime.arm_forced`, and scenario buttons with *force arm on open zones* on | — | always the cgi (see below) |

A scenario goes native only when every target maps to one mode: *away* → Total, *stay* → Partial, *disarm* → Disarmed. Anything else (e.g. a target set to both away and stay) is applied by the cgi. Scenario definitions are decoded for partitions 0–11 only, so on a panel with an area 12 or higher every scenario uses the cgi: a scenario could target a partition the decode cannot see, and a native apply would then leave it out.

**Arming readiness.** The cgi refuses to arm with open zones (*zones not ready*); how the native command treats open zones is not known, and **op 3 may well force-arm**. So before a native arm, the zones of every area being armed are read live over the same connection, under the same lock, right before the command is sent (not from cached or webhook-patched state). If one of them is neither ready nor bypassed, or is not reported, nothing is sent and the arm goes to the cgi, which applies the panel's own check and answers *zones not ready* exactly as with the option off. This works with fast polling off too. Arm **home** therefore goes to the cgi whenever any zone of the area is open, even an internal zone that partial arming skips. Disarming needs no check. If a zone's areas are unknown (no native zone map, or a zone the map does not list), arming always uses the cgi.

**Not checked on the native path.** Only zone readiness is checked. Tamper, faults, the area's alarm state and alarm memory are **not**, whereas the cgi applies whatever checks the panel's cgi applies. The user code typed in Home Assistant is not used either: native commands carry no PIN (the alarm panels do not ask for a code, as before).

**Forced arming stays on the cgi.** Its open-zone check is the cgi's `get_*_nrz`, which knows which zones each arming mode and scenario uses. The native zone state has no per-mode view, and mixing channels between the check, the bypasses, the verification and the rollback would give the rollback two sources of truth.

## Safety rules

- **Explicit allow-list.** Only four write opcodes can ever be built (3, 8, 9, 16), in their own allow-list, separate from the read-only status commands (6, 7) and the memory reads. The checks are explicit `if … raise`, not `assert`, so `python -O` cannot remove them.
- **Sent at most once, never retried blindly.** A command is retried on the cgi only when it **certainly never left**: the connection could not be opened, or the arguments were refused before sending. Once the frame has been handed to the socket, any failure (timeout, dropped connection, unexpected answer) means the panel **may have executed it**: nothing is retried, the state is re-read, and Home Assistant shows *"The native command may have reached the panel, but its outcome is unknown… check the panel state before trying again."*
- **Liveness check first.** Before a write on the kept-open connection, a read-only partition-status read proves the connection alive. A dead connection is then replaced before the write, instead of the write disappearing into it.
- **Same connection, same lock.** Writes share the persistent connection and lock of the native poll, so a write never interleaves with a status read. Each write has a 15 s ceiling; the liveness read inside it has its own 5 s ceiling, so a stalled connection is replaced with time left for the reconnect and the write.
- **Quick feedback.** After a native write the area and zone state is re-read natively at once when fast polling is on, and the usual cgi refresh follows. With fast polling off, only the cgi refresh runs (logged once at setup).

## What the panel answers

Pitscheider's library does not decode command responses, so there is no documented success or error code. On a PrimeX 4.07 the read-only status commands answer with an 18-byte header that starts with the bitwise NOT of the opcode (`f9 ff ff ff` for op 6), followed by `01 00 ff ff ff 03 00 00 ff ff ff ff ff ff`. A write response is only checked for that opcode echo; a different answer is reported as *may have reached the panel*. The header of every write answer is logged at debug level (`custom_components.inim_prime.client.local6004`), so live testing can tell whether a rejection looks different.

## Frames

A command is `[op:4 LE][PIN:6][data]`, AES-encrypted in the usual 6004 frame with flag `01 00`. The PIN field is `74 00 00 00 00 00` (no PIN); the integration never sends a PIN. The client can encode one (one digit per byte, padded with `ff`) should a panel require it.

| Command | Data | Example (plaintext body) |
|---|---|---|
| op 3 set arming status | 30 bytes, one per partition (0-based): 1 Total, 2 Partial, 3 Instant/snapshot, 4 Disarmed, 0 untouched | area 4 → Total: `03000000 740000000000` + `00000000 01` + 25 × `00` |
| op 9 set zone bypass | `[zone:2 LE][0 bypass / 2 unbypass:2 LE]` | bypass zone 12: `09000000 740000000000 0c00 0000` |
| op 8 set output | `[terminal:2 LE][1 on / 0 off:2 LE]` | output 1007 on: `08000000 740000000000 ef03 0100` |
| op 16 reset partitions | `[partition bitmask:4 LE]` | reset area 4: `10000000 740000000000 10000000` |

The unit tests pin these bodies byte for byte to the output of Pitscheider's own payload builders.

## Testing it safely

Test at home, with the keypad or the INIM app at hand, one step at a time. Before each step, check the entity's state; after it, check both Home Assistant and the keypad.

1. **Enable debug logging** for `custom_components.inim_prime` (to capture the answer headers).
2. Turn on **Native commands** in the integration options (the integration reloads).
3. **Clear alarm memory** on an area that has none (least risky: nothing to reset).
4. **Bypass a zone that is already bypassed**, or bypass then un-bypass a closed zone of a disarmed area. Check the bypass switch and the keypad.
5. **Arm and disarm one low-risk area** (e.g. an outbuilding such as *Box*), away first, with all its zones closed, and disarm right away. Then try arm home, and arm **night**: check on the keypad that night arrives as the panel's *instant* mode (native value 3, the cgi's "snapshot"), since that mapping is not verified.
6. **Open a zone of that area** and try to arm away: it must go to the cgi and fail with *zones not ready* (debug log: "not sent … zones not ready").
7. **Apply one single-area scenario** whose targets are plain away/stay/disarm.
8. **Outputs last**, starting with one that drives nothing audible (outputs can drive sirens).

If anything looks wrong (a command that seems not to act, an error, a state that does not match the keypad), stop, turn the option off, and report it with the debug log.

## Rolling back

Turn **Native commands** off in the options. The integration reloads and every write goes through the cgi again, exactly as before. Nothing is stored on the panel.
