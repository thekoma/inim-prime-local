# Firmware EEPROM offset recovery

The native TCP 6004 client (`custom_components/inim_prime/client/local6004.py`)
reads the panel's config memory at fixed EEPROM addresses that differ per
firmware family (see `_Layout` / `_LAYOUTS`). Those addresses used to be
hand-recovered magic numbers. They are not magic: they live, by name, in the
official **Prime/STUDIO** software, and this tool reproduces them.

## Where the addresses come from

The `Centrale*.dll` files shipped with Prime/STUDIO
(`C:\Program Files (x86)\Inim\Prime\Centrale5.dll`, `Centrale6.dll`) are **.NET
assemblies**. The tool reads them as ECMA-335 metadata + IL only — it never
runs any code from them, never touches the panel, and needs no .NET runtime,
VM, or network.

Two self-describing sources inside the DLL give every address a stable,
human-readable name:

1. **`maps_<NNN>::getValue("key")`** — one class per firmware
   (`maps_400` -> 4.00, `maps_500` -> 5.00, also 1.00/3.00/3.10). It is a
   compiler-generated `switch(string)` that returns the address as an IL
   immediate. Config fields:
   - `zone_cfg`      = `getValue["spring_exf_prg_zones"]`
   - `scenario_modi` = `getValue["spring_exf_prg_scenari"]`
   - `log_addr`      = `getValue["spring_eep_logger_ev"]` (low-eeprom region)
   - (+ ~150 other EEPROM fields, all self-named by the firmware)
2. **`MemoryCopy::InizializzaIndirizziStringhe`** — the label-base addresses,
   written as `stfld AddressXXX` in version-gated blocks:
   - `area_labels` = `AddressAREE`, `zone_labels` = `AddressTERMINALI`,
     `scenario_labels` = `AddressMODINS`, `output_labels` = `AddressUSCITE`

Record **counts** are derived from the DLL by family-scoped span arithmetic
(`base + count*record_size + 2`-byte CRC trailer); record **sizes** stay
constants in `local6004.py`.

> The `04 .. .. 00 10 04` byte marker that an earlier manual pass keyed on is a
> red herring -- a separate positional `int32[]` cache. Do not use it; the
> authoritative source is `getValue` / `InizializzaIndirizziStringhe`.

## Files

- `extract_offsets.py` -- the extractor (pure Python, stdlib only, read-only).
- `dotnetmeta.py` -- minimal ECMA-335 PE/CLI metadata reader.
- `ildis.py` -- minimal IL disassembler.
- `data/centrale{5,6}.offsets.json` -- committed extractor output (the audit
  artifact). The proprietary DLLs themselves are **never committed**; a
  maintainer runs the tool against their own copy.

## Usage

```sh
# Validate that the tool reproduces the offsets we rely on:
python3 tools/offsets/extract_offsets.py /path/to/Centrale5.dll --validate

# Regenerate the committed audit artifact after a new Prime/STUDIO release:
python3 tools/offsets/extract_offsets.py /path/to/Centrale5.dll --json tools/offsets/data/centrale5.offsets.json
python3 tools/offsets/extract_offsets.py /path/to/Centrale6.dll --json tools/offsets/data/centrale6.offsets.json
```

## How the code stays honest

`tests/test_offset_provenance.py` is in the normal test suite. For every mapped
offset in `_LAYOUTS` it asserts the address matches the committed JSON, that the
DLL-derived record counts agree with the layout (`zone_cfg` count ==
`2 * half_offset`, 50 scenarios), and that each offset carries full provenance
(symbol, source method, DLL file offset, raw IL bytes). A code offset can no
longer drift from its DLL-attested value without the test failing.

## Adding a new firmware family

1. Get the matching `Centrale*.dll` from a Prime/STUDIO install.
2. `--validate` it, then `--json` to refresh `data/`.
3. Add a `_LAYOUT_<nn>X` to `local6004.py` using the JSON addresses (with the
   symbol name in the provenance comment) and register it in `_LAYOUTS`.
4. The provenance test then guards the new family too.

New native **write** support for an unverified family must stay gated
(`native_writes_ok=False`) until confirmed on live hardware -- the offsets being
DLL-attested is necessary but not sufficient for write safety.
