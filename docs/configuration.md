# Configuration

## What you need from the panel
Nothing needs enabling on the panel: Home Assistant talks to it over its native LAN protocol (TCP port 6004), the same channel Prime/STUDIO uses, which is always on. You need:

1. the panel's **IP address** (or the PrimeLAN board's);
2. its **LAN password**, the one Prime/STUDIO asks for to connect over the network (factory default `pass`; change it in Prime/STUDIO if it is still the default).

Supported firmware: PrimeX/Prime **4.x** and **5.00**. On any other firmware family setup stops with *firmware not supported*, because the panel's memory map (where names and settings live) differs between families.

## Add the integration
**Settings → Devices & Services → Add Integration → INIM Prime**, then fill in:

| Field | Example | Notes |
|---|---|---|
| Name | `Allarme` | Used for the device and entity names |
| Host | `192.168.1.50` | Panel IP (or PrimeLAN IP) |
| Panel LAN password | `pass` | The panel's LAN/programming password |

Setup reads the panel's structure once: which areas, zones, scenarios and outputs exist, their names and how each zone is programmed. On success you get one **device** with entities for every area with a real name, every zone, scenario and output. Unused factory-default areas (`AREA 006`…) get no entities, and the output switches are created **disabled** — enable any you need from the device page.

## Choosing which areas to show
Open the integration → **Configure** and pick the panel areas under **Areas to show**. Each selected area gets an alarm panel, an alarm-memory sensor and a clear-memory button; an unselected one gets no entities at all (ones left from before are removed from the registry).

- The choice is kept by **area number**, not by name: renaming an area on the panel keeps it, and the entity name follows the new label.
- A hidden area still works: scenarios and commands that also set it run exactly as before.
- Until you save a choice, every area with a real name is shown. After that, an area added on the panel later stays hidden (with a warning in the log) until you select it.
- Selecting an area again creates its entities afresh, without any custom name or room you had given them.

## Changing the password or the address
Open the integration → **⋮ → Reconfigure** to change the name, the host or the LAN password. No need to delete and re-add. A wrong password cannot be told apart from an unreachable panel, so after changing it in Prime/STUDIO update it here too.

## Upgrading from a version that used the HTTP API
Versions before 2026.10.7 also read the panel through its HTTP API (`api.cgi`, with an API key). On the first start after the upgrade the entry is migrated:

- the API key, port, HTTPS and polling settings are dropped, and the entry is identified by its host alone;
- every entity keeps its id and history;
- the three API diagnostic sensors (*API version*, *API connections*, *Last API client*) are removed, since only the HTTP API could fill them.

Nothing else is needed: those versions already required the LAN password. If nothing else uses the panel's HTTP API, you can turn it off in Prime/STUDIO.

## Removing
Delete the integration entry from Devices & Services. All entities and the device are removed.
