# Configuration

## Prerequisites: enable the local API on the panel
1. In **PrimeStudio**, open the **PrimeLAN** configuration page.
2. Enable the **HTTP API** and click **Generate** to obtain an **API key**.
3. Note the panel's **IP address** and **port** (default `8080`). Keep "Only allow connections via HTTPS" off unless your PrimeLAN board supports it.
4. Recommended: set the PrimeLAN **IP/MAC allow-list** to accept the API only from your Home Assistant host.

## Add the integration
**Settings → Devices & Services → Add Integration → INIM Prime**, then fill in:

| Field | Example | Notes |
|---|---|---|
| Host | `192.168.1.50` | Panel IP (or PrimeLAN IP) |
| Port | `8080` | Default |
| API key | `xxxxxxxx-…` | Generated in PrimeStudio |
| Use HTTPS | unchecked | Only if the board has HTTPS enabled |

On success you get one **device** with entities for every configured area, zone, scenario, output and the panel diagnostics. Unused factory-default areas and the output switches are created **disabled** — enable any you need from the device page.

## Rotating the API key
Generate a new key in PrimeStudio, then in Home Assistant open the integration → **⋮ → Reconfigure** and enter the new key (host/port/HTTPS can be changed here too). No need to delete and re-add.

## Removing
Delete the integration entry from Devices & Services. All entities and the device are removed.
