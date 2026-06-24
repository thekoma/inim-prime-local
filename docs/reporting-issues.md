# Reporting issues

Please open issues at <https://github.com/thekoma/inim-prime-local/issues>. To help diagnose quickly, include:

1. **Panel model and firmware** — e.g. PrimeX, firmware `4.07` (shown on the device page).
2. **Home Assistant version** and how you installed the integration (HACS / manual) + its version.
3. **What you expected vs. what happened**, and which entity/area/zone is involved.
4. **Debug logs.** Add this to `configuration.yaml`, restart, reproduce, then attach the log:
   ```yaml
   logger:
     default: warning
     logs:
       custom_components.inim_prime: debug
   ```
5. **Diagnostics.** Device page → **⋮ → Download diagnostics** (the API key is redacted automatically).

> ⚠️ **Never paste your API key** or full unredacted URLs. Scrub them from logs before attaching. The downloaded diagnostics already redact the key.

For realtime/webhook setup questions, see [realtime.md](realtime.md); for polling behaviour see [polling-and-tuning.md](polling-and-tuning.md).
