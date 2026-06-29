# INIM brand assets

Official INIM logo (wordmark, navy `#0E4174`) taken from the manufacturer site
(`inim.it` → `logo-inim.svg`), rasterised and prepared to the Home Assistant
[brands](https://github.com/home-assistant/brands) spec — NOT a fabricated logo.

- `custom_integrations/inim_prime/icon.png` (256×256) + `icon@2x.png` (512×512) — square, the wordmark centred on transparent.
- `custom_integrations/inim_prime/logo.png` (256×120) + `logo@2x.png` (512×240) — trimmed wordmark, transparent.
- `inim-logo-official.svg` — the official source SVG (provenance).

## How HA actually shows it
Home Assistant fetches integration images from `brands.home-assistant.io`, so the
PNGs above must be submitted to the `home-assistant/brands` repo under
`custom_integrations/inim_prime/`. Until that PR is merged, HA shows a generic icon.
