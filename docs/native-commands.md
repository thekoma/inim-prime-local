# Native commands

Every write (arm/disarm, scenarios, zone bypass, outputs, alarm-memory reset, forced arming) goes over the panel's native TCP 6004 protocol, the same connection the 2 s status poll uses. There is no other path and no option.

> **Verified live on PrimeX 4.07 and 5.00.** The command layouts follow [Pitscheider/inim-prime-native](https://github.com/Pitscheider/inim-prime-native) (GPL-3.0) and Prime/STUDIO's own frames, and have been run against a live panel.

## Commands

| Action | Native command | Refused (nothing sent) when |
|---|---|---|
| Disarm an area | op 3 (set arming status) | — |
| Arm an area (away / home / night) | op 3 | a zone that matters is neither ready nor bypassed (see below) |
| Apply a scenario (button) | op 3, all target areas in one command | a zone that matters is not ready, or the scenario is not supported (see below) |
| Zone bypass switch | op 9 (set zone bypass) | — |
| Output switch | op 8 (set output) | — |
| Clear alarm memory button | op 16 (reset partitions) | — |
| `inim_prime.arm_forced`, and scenario buttons with *force arm on open zones* on | op 9 for each open zone, then op 3 | an open zone cannot be bypassed and `allow_partial` is off |

A scenario is sent as one op 3 when every target maps to one mode: *away* → Total, *stay* → Partial, *disarm* → Disarmed. A scenario with any other target (e.g. an area set to both away and stay) is refused with *scenario not supported*. Scenario definitions are decoded for partitions 0–11 only, so on a panel with an area 12 or higher every scenario is refused: it could target a partition the decode cannot see, and applying it would leave that partition out.

**Arming readiness.** How the panel treats an op 3 arm with open zones is not documented, and **op 3 may well force-arm**. So before an arm, the zones that matter are read live over the same connection, under the same lock, right before the command is sent (not from cached or webhook-patched state). If one of them is neither ready nor bypassed, or is not reported, nothing is sent and Home Assistant shows *zones not ready*. The zones that matter are those of the areas being armed, less the ones the panel itself leaves out of that arming: zones programmed *Autoescludibile* (the panel bypasses them when open) and zones programmed *Interna* when every area being armed that they belong to goes to home (partial) or night (instant). Disarming needs no check.

**Not checked.** Only zone readiness is checked. Tamper, faults, the area's alarm state and alarm memory are **not**. The user code typed in Home Assistant is not used either: commands carry no PIN (the alarm panels do not ask for a code).

**Bypass read-back.** The panel acknowledges a bypass it does not apply (e.g. on a zone programmed *Non escludibile*), so after op 9 the zone is read back up to 4 times, 0.5 s apart. A change that never shows up is reported as *the panel did not apply the bypass change* instead of the switch silently flipping back. Zones programmed *Non escludibile* get no bypass switch at all.

**Forced arming.** The zones that matter (as above) are read live; each open one that can be bypassed is bypassed with op 9, all of them are read back, and only then is op 3 sent, without a second readiness check. If a zone could not be bypassed and `allow_partial` is off, the bypasses already applied are undone and nothing is armed. If the arm certainly did not reach the panel, the bypasses are undone too; if its outcome is uncertain they are kept (re-including an open zone of an armed area would trip it).

## Safety rules

- **Explicit allow-list.** Only four write opcodes can ever be built (3, 8, 9, 16), in their own allow-list, separate from the read-only status commands (6, 7, 0x0D) and the memory reads. The checks are explicit `if … raise`, not `assert`, so `python -O` cannot remove them.
- **Sent at most once, never retried.** If the frame **certainly never left** (the connection could not be opened, or the arguments were refused before sending), Home Assistant shows *the panel command failed*. Once the frame has been handed to the socket, any failure (timeout, dropped connection, unexpected answer) means the panel **may have executed it**: nothing is retried, the state is re-read, and Home Assistant shows *"The native command may have reached the panel, but its outcome is unknown… check the panel state before trying again."*
- **Liveness check first.** Before a write on the kept-open connection, a read-only partition-status read proves the connection alive. A dead connection is then replaced before the write, instead of the write disappearing into it.
- **Same connection, same lock.** Writes share the persistent connection and lock of the status poll, so a write never interleaves with a status read. Each write has a 15 s ceiling; the liveness read inside it has its own 5 s ceiling, so a stalled connection is replaced with time left for the reconnect and the write.
- **Quick feedback.** After a write the whole state is re-read at once, instead of waiting for the next 2 s cycle.

## What the panel answers

Pitscheider's library does not decode command responses, so there is no documented success or error code. On a PrimeX 4.07 the read-only status commands answer with an 18-byte header that starts with the bitwise NOT of the opcode (`f9 ff ff ff` for op 6), followed by `01 00 ff ff ff 03 00 00 ff ff ff ff ff ff`. A write response is only checked for that opcode echo; a different answer is reported as *may have reached the panel*. The header of every write answer is logged at debug level (`custom_components.inim_prime.client.local6004`). On a 5.00 a bypass answer was seen to carry the result in byte 18 (`02` bypassed, `03` un-bypassed, `05` refused); the integration does not rely on it and reads the zone back instead.

## Frames

A command is `[op:4 LE][PIN:6][data]`, AES-encrypted in the usual 6004 frame with flag `01 00`. The PIN field is `74 00 00 00 00 00` (no PIN); the integration never sends a PIN. The client can encode one (one digit per byte, padded with `ff`) should a panel require it.

| Command | Data | Example (plaintext body) |
|---|---|---|
| op 3 set arming status | 30 bytes, one per partition (0-based): 1 Total, 2 Partial, 3 Instant/snapshot, 4 Disarmed, 0 untouched | area 4 → Total: `03000000 740000000000` + `00000000 01` + 25 × `00` |
| op 9 set zone bypass | `[terminal:2 LE][0 bypass / 2 unbypass, +1 for the terminal's second zone:2 LE]` | bypass zone 12: `09000000 740000000000 0c00 0000`; on 5.00 bypass zone 1521 (terminal 16, second zone): `… 1000 0100` |
| op 8 set output | `[terminal:2 LE][1 on / 0 off:2 LE]` | output 1007 on: `08000000 740000000000 ef03 0100` |
| op 16 reset partitions | `[partition bitmask:4 LE]` | reset area 4: `10000000 740000000000 10000000` |

The unit tests pin these bodies byte for byte to the output of Pitscheider's own payload builders.

## Checking it on your panel

Commands act on a real alarm: test at home, with the keypad or the INIM app at hand, one step at a time, and check both Home Assistant and the keypad after each step.

1. **Enable debug logging** for `custom_components.inim_prime` (to capture the answer headers).
2. **Clear alarm memory** on an area that has none (least risky: nothing to reset).
3. **Bypass then un-bypass a closed zone** of a disarmed area. Check the bypass switch and the keypad.
4. **Arm and disarm one low-risk area** (e.g. an outbuilding), away first, with all its zones closed, and disarm right away. Then try arm home, and arm **night**: check on the keypad that night arrives as the panel's *instant* mode (native value 3).
5. **Open a zone of that area** and try to arm away: it must fail with *zones not ready* and nothing must change on the keypad.
6. **Apply one single-area scenario** whose targets are plain away/stay/disarm.
7. **Outputs last**, starting with one that drives nothing audible (outputs can drive sirens).

If anything looks wrong (a command that seems not to act, an error, a state that does not match the keypad), stop and report it with the debug log.
