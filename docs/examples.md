# Examples & use cases

A few ways people use this integration. Entity IDs below are illustrative — use your own (they're derived from your area/zone names).

## Notify when the alarm is triggered
```yaml
automation:
  - alias: "Alarm triggered → push notification"
    trigger:
      - trigger: state
        entity_id: alarm_control_panel.appartam_giorno
        to: "triggered"
    action:
      - action: notify.mobile_app_phone
        data:
          title: "🚨 Alarm!"
          message: "{{ trigger.to_state.name }} is in alarm."
```

## Turn lights on when you arm "night" (and off when disarmed)
```yaml
automation:
  - alias: "Arm night → hallway light low"
    trigger:
      - trigger: state
        entity_id: alarm_control_panel.appartam_notte
        to: "armed_night"
    action:
      - action: light.turn_on
        target: { entity_id: light.hallway }
        data: { brightness_pct: 10 }
```

## Alert on a power/mains fault
Enable the diagnostic per-fault binary sensors (disabled by default), then:
```yaml
automation:
  - alias: "Mains fault → notify"
    trigger:
      - trigger: state
        entity_id: binary_sensor.mains_fault
        to: "on"
    action:
      - action: notify.family
        data: { message: "INIM panel reports a mains power fault." }
```

## Warn if a window is still open when you try to arm
```yaml
automation:
  - alias: "Open zones before arming"
    trigger:
      - trigger: state
        entity_id: alarm_control_panel.esterno
        to: "arming"
    condition:
      - condition: numeric_state
        entity_id: sensor.open_zones
        above: 0
    action:
      - action: notify.family
        data: { message: "{{ states('sensor.open_zones') }} zone(s) still open." }
```

## Use cases
- **Presence-aware arming** — arm *away* when everyone leaves (combine with person/device-tracker), disarm when someone arrives.
- **Scheduled night arming** — arm *night* at bedtime via a time trigger; the panel's own scenarios remain authoritative.
- **Perimeter dashboard** — group the zone `binary_sensor`s (doors/windows/motion) on a floor-plan card.
- **Tamper / fault monitoring** — surface the system-fault and per-fault sensors on a maintenance dashboard.
- **Instant alerts** — pair with [realtime push](realtime.md) so alarm/arm events reach you in well under a second, fully on your LAN.
