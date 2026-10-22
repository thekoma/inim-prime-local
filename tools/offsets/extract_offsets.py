#!/usr/bin/env python3
"""
extract_offsets.py  --  Reproducible INIM Prime/STUDIO EEPROM offset extractor.

Given an INIM "Centrale*.dll" (.NET assembly), recover, per firmware version, the
EEPROM field -> address map that the official software uses. No magic numbers:
addresses are read straight from the firmware's own named symbol table and the
label-init routine inside the DLL.

Two deterministic sources inside the DLL (read-only, metadata + IL, no execution):

  1. maps_<NNN>::getValue(key)  -- one class per firmware (NNN = e.g. 500 -> v5.0).
     It is a compiler-generated switch(string): hash(key) dispatch -> string
     equality -> a result block `ldc.i4 <addr>; stloc.0`. We join name->addr.
     => zone_cfg      = getValue["spring_exf_prg_zones"]
        scenario_modi = getValue["spring_exf_prg_scenari"]
        (+150-odd other EEPROM-region fields, all self-named by the firmware)

  2. MemoryCopy::InizializzaIndirizziStringhe -- label base addresses, written as
     `ldarg.0; ldc.i4 <addr>; stfld <AddressXXX>`, in version-gated blocks
     (Substring(ver) == "1.0"/"2.0"/"3.0"/"3.1"/"4.0"/"5.0" -> block).
     => area_labels     = AddressAREE
        zone_labels     = AddressTERMINALI   (INIM "terminali" == zone terminals)
        scenario_labels = AddressMODINS
        output_labels   = AddressUSCITE

Usage:
  python3 extract_offsets.py <Centrale.dll> [--json out.json] [--validate]
"""
import sys, os, json, argparse, hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ildis import load, find_method, body_info, disasm, us_string
from dotnetmeta import T

TOOL_VERSION = "1.0.0"
SCHEMA_VERSION = "1"

# --- integration-facing aliases ---------------------------------------------
CONFIG_ALIASES = {                      # friendly name -> getValue symbol
    "zone_cfg":      "spring_exf_prg_zones",
    "terminal_cfg":  "spring_exf_prg_terminals",
    "pulse_cfg":     "spring_exf_pulse_settings",
    "zone_opt2":     "spring_exf_opt_zone_1",
    "scenario_modi": "spring_exf_prg_scenari",
    # event log read via the low-address path (op 0x1f). Matches the repo's
    # _LOG_ADDR 0xA1D0 on 4.00 exactly. 5.00 = 0x104A8, same 0x1f path.
    # NOTE: spring_eep_logger_ev (low region), NOT spring_exf_eventi (config region).
    "log_addr":      "spring_eep_logger_ev",
}
LABEL_ALIASES = {                       # friendly name -> MemoryCopy field
    "area_labels":     "AddressAREE",
    "zone_labels":     "AddressTERMINALI",
    "scenario_labels": "AddressMODINS",
    "output_labels":   "AddressUSCITE",
}
# record sizes (protocol constants) + 2-byte CRC trailer, for count recovery
RECORD = {"zone_cfg": 11, "terminal_cfg": 12, "pulse_cfg": 28, "zone_opt2": 1,
          "scenario_modi": 19}
BLOB = {"log_addr"}                      # length-only (no fixed record size)
CRC_TRAILER = 2
# Tables packed without the CRC trailer: their span is an exact record multiple
# (terminal_cfg = 1005 x 12 on 4.00, 1505 x 12 on 5.00; pulse_cfg = 1010 x 28
# and 1510 x 28, one record per terminal including the outputs).
NO_TRAILER = {"terminal_cfg", "pulse_cfg"}
# region high-byte -> panel context opcode used to read it (from the integration).
# 0x14 = external-flash/config region (_OPEN_CFG 0x17); 0x00 = low EEPROM (_OPEN_LOG 0x1f).
REGION_OP = {0x14: 0x17, 0x00: 0x1f}
REGION_NAME = {0x14: "config/external-flash", 0x00: "low-eeprom",
               0x10: "ram", 0x1A: "const", 0x20: "ram2"}


def _family(symbol):
    """'spring_exf_prg_zones' -> 'spring_exf_' (packing family for span math)."""
    parts = symbol.split("_")
    return "_".join(parts[:2]) + "_" if len(parts) >= 2 else symbol


def _next_in_family(symbol, addr, gv_addr):
    """Next address strictly above `addr` among symbols of the same packing family."""
    fam = _family(symbol)
    region = addr >> 24
    return min((a for n, a in gv_addr.items()
                if n.startswith(fam) and (a >> 24) == region and a > addr), default=None)


def firmware_version(maps_classname):
    """maps_500 -> '5.00', maps_310 -> '3.10', maps_100 -> '1.00'."""
    nnn = int(maps_classname.split("_")[1])
    return f"{nnn // 100}.{nnn % 100:02d}"


def _match_version(verstr, gate_strings):
    """'5.00' matches a label gate like '5.0'."""
    a = verstr.rstrip("0").rstrip(".")
    for g in gate_strings:
        b = g.rstrip("0").rstrip(".")
        if a == b:
            return True
    return False


# --- source 1: maps_NNN::getValue -------------------------------------------
def extract_getvalue(data, pe, m, typ):
    """Return (name -> {addr, il_off}, body_file_start)."""
    mi, mr = find_method(m, typ, "getValue")
    if mi is None:
        return {}, None
    il, body_file = body_info(data, pe, mr[0])
    ins = disasm(il, m)
    # result blocks: entry IL offset of `ldc.i4 V ; stloc.0` -> (V, il_off)
    res = {}
    for i in range(len(ins) - 1):
        s, t, v = ins[i]
        if t.startswith("ldc.i4 ") and ins[i + 1][1] == "stloc.0":
            res[s] = (v & 0xFFFFFFFF, s)
    out = {}
    for i in range(len(ins) - 1):
        s, t, v = ins[i]
        if not t.startswith("ldstr "):
            continue
        name = us_string(m, v)
        for j in range(i + 1, min(i + 5, len(ins))):
            tj, vj = ins[j][1], ins[j][2]
            if tj.startswith(("brtrue", "brfalse", "beq", "bne.un", "br ")) and isinstance(vj, int):
                if vj in res:
                    addr, il_off = res[vj]
                    out[name] = {"addr": addr, "il_off": il_off}
                break
    return out, body_file


# --- source 2: MemoryCopy::InizializzaIndirizziStringhe ----------------------
def field_name(m, tok):
    if (tok >> 24) != 0x04:
        return None
    return m.strings(m.row(T['Field'], tok & 0xFFFFFF)[1])


def extract_label_blocks(data, pe, m):
    """Return ({version_gate: {AddressXXX: {addr, il_off}}}, body_file_start)."""
    mi, mr = find_method(m, "MemoryCopy", "InizializzaIndirizziStringhe")
    if mi is None:
        return {}, None
    il, body_file = body_info(data, pe, mr[0])
    ins = disasm(il, m)
    # gate: ldstr "X.Y" ; ... ; br* -> TARGET  (collect version -> target offset)
    ver_target = {}
    for i, (s, t, v) in enumerate(ins):
        if t.startswith("ldstr "):
            sv = us_string(m, v)
            if sv and len(sv) <= 4 and sv[0].isdigit() and "." in sv:
                for j in range(i + 1, min(i + 4, len(ins))):
                    tj, vj = ins[j][1], ins[j][2]
                    if tj.startswith(("brtrue", "beq")) and isinstance(vj, int):
                        ver_target[sv] = vj
                        break
    # block starts, sorted; each assignment belongs to the latest start <= its offset
    starts = sorted(set(ver_target.values()))
    assigns = []  # (il_off, fieldname, addr)
    for i in range(len(ins) - 1):
        s, t, v = ins[i]
        if t.startswith("ldc.i4 ") and ins[i + 1][1].startswith("stfld"):
            fn = field_name(m, ins[i + 1][2])
            if fn:
                assigns.append((s, fn, v & 0xFFFFFFFF))

    def block_of(off):
        b = None
        for st in starts:
            if st <= off:
                b = st
            else:
                break
        return b
    blocks = {st: {} for st in starts}
    for off, fn, addr in assigns:
        b = block_of(off)
        if b is not None:
            blocks[b][fn] = {"addr": addr, "il_off": off}
    # map each version string to its block's fields
    return {ver: blocks.get(tgt, {}) for ver, tgt in ver_target.items()}, body_file


# --- assembly ----------------------------------------------------------------
def _provenance(data, body_file, il_off):
    """Absolute DLL file offset of the ldc.i4 opcode + its 5 raw bytes (20 + LE32)."""
    if body_file is None or il_off is None:
        return None, None
    foff = body_file + il_off
    return foff, " ".join(f"{b:02x}" for b in data[foff:foff + 5])


def extract(path):
    data, pe, m = load(path)
    n_type = m.rows[T['TypeDef']]
    maps_classes = []
    for i in range(1, n_type + 1):
        nm = m.strings(m.row(T['TypeDef'], i)[1])
        if nm.lower().startswith("maps_") and nm.split("_", 1)[1].isdigit():
            maps_classes.append(nm)
    label_blocks, lbl_body = extract_label_blocks(data, pe, m)

    firmwares = {}
    for cls in sorted(maps_classes, key=lambda c: int(c.split("_")[1])):
        ver = firmware_version(cls)
        gv, gv_body = extract_getvalue(data, pe, m, cls)   # name -> {addr, il_off}
        labels = {}
        for gate, flds in label_blocks.items():
            if _match_version(ver, [gate]):
                labels = flds
                break
        gv_addr = {n: e["addr"] for n, e in gv.items()}
        fields = {}
        # config fields (maps_NNN::getValue, keyed by firmware symbol name)
        for friendly, sym in CONFIG_ALIASES.items():
            if sym in gv:
                addr = gv[sym]["addr"]
                foff, ilb = _provenance(data, gv_body, gv[sym]["il_off"])
                region = addr >> 24
                entry = {"addr": f"0x{addr:08X}", "symbol": sym,
                         "source": f"{cls}::getValue", "method": f"{cls}::getValue",
                         "file_offset": f"0x{foff:X}" if foff is not None else None,
                         "il_bytes": ilb,
                         "region": REGION_NAME.get(region, f"0x{region:02X}xxxxxx"),
                         "context_op": (f"0x{REGION_OP[region]:02X}"
                                        if region in REGION_OP else None)}
                nxt = _next_in_family(sym, addr, gv_addr)
                if friendly in RECORD and nxt is not None:   # record table -> count
                    span = nxt - addr
                    rec = RECORD[friendly]
                    trailer = 0 if friendly in NO_TRAILER else CRC_TRAILER
                    entry.update(record_size=rec, span_bytes=span,
                                 count=(span - trailer) // rec,
                                 count_source=f"derived: (span-{trailer})//{rec}, next={_family(sym)}* field")
                elif friendly in BLOB and nxt is not None:    # blob -> length only
                    entry.update(length_bytes=nxt - addr,
                                 length_source=f"derived: span to next {_family(sym)}* field")
                fields[friendly] = entry
        # label fields (MemoryCopy::InizializzaIndirizziStringhe, version-gated)
        for friendly, fld in LABEL_ALIASES.items():
            if fld in labels:
                addr = labels[fld]["addr"]
                foff, ilb = _provenance(data, lbl_body, labels[fld]["il_off"])
                region = addr >> 24
                fields[friendly] = {
                    "addr": f"0x{addr:08X}", "symbol": fld,
                    "source": f"MemoryCopy::{fld}",
                    "method": "MemoryCopy::InizializzaIndirizziStringhe",
                    "file_offset": f"0x{foff:X}" if foff is not None else None,
                    "il_bytes": ilb, "record_size": 16,
                    "region": REGION_NAME.get(region, f"0x{region:02X}xxxxxx"),
                    "context_op": (f"0x{REGION_OP[region]:02X}"
                                   if region in REGION_OP else None)}
        firmwares[ver] = {
            "maps_class": cls,
            "fields": fields,
            # raw material for auditing / future fields (addresses only):
            "label_fields_all": {k: f"0x{v['addr']:08X}" for k, v in labels.items()},
            "config_symbols_all": {k: f"0x{v['addr']:08X}" for k, v in gv.items()},
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "extract_offsets.py",
        "tool_version": TOOL_VERSION,
        "dll": os.path.basename(path),
        "dll_sha256": hashlib.sha256(data).hexdigest(),
        "firmwares": firmwares,
    }


# --- validation --------------------------------------------------------------
KNOWN = {
    "4.00": {"area_labels": 0x14030D20, "zone_labels": 0x14030F00,
             "scenario_labels": 0x1403D8E0, "output_labels": 0x1403DEB0,
             "zone_cfg": 0x14073414, "scenario_modi": 0x1407DFA8},
    "5.00": {"area_labels": 0x14030D20, "zone_labels": 0x14030F00,
             "scenario_labels": 0x14041760, "output_labels": 0x14041D30,
             "zone_cfg": 0x1408B920, "scenario_modi": 0x14099368},
}


def validate(result):
    ok = True
    firmwares = result["firmwares"]
    for ver, known in KNOWN.items():
        fw = firmwares.get(ver)
        print(f"\n[validate] firmware {ver}  (class {fw['maps_class'] if fw else '??'})")
        if not fw:
            print("   MISSING firmware block!"); ok = False; continue
        for name, exp in known.items():
            gs = fw["fields"].get(name, {}).get("addr")
            got = int(gs, 16) if gs else None
            status = "OK " if got == exp else "FAIL"
            if got != exp:
                ok = False
            print(f"   {status} {name:16} expected 0x{exp:08X}  got "
                  + (f"0x{got:08X}" if got is not None else "None"))
    # log_addr ground truth: repo reads 4.x log at 0xA1D0, len 56000 (op 0x1f)
    lf = firmwares.get("4.00", {}).get("fields", {}).get("log_addr", {})
    la = int(lf["addr"], 16) if lf.get("addr") else None
    ll = lf.get("length_bytes")
    log_ok = (la == 0xA1D0 and ll == 56000 and lf.get("context_op") == "0x1F")
    ok = ok and log_ok
    print(f"\n[validate] log_addr 4.00 (repo _LOG_ADDR 0xA1D0, len 56000, op 0x1f): "
          f"{'OK' if log_ok else 'FAIL'}  got 0x{la:X} len={ll} op={lf.get('context_op')}")
    print("\n[validate] RESULT:", "ALL KNOWN OFFSETS REPRODUCED" if ok else "MISMATCH")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dll")
    ap.add_argument("--json")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--full", action="store_true", help="dump full symbol tables")
    args = ap.parse_args()
    result = extract(args.dll)
    print(f"# {result['dll']}  sha256={result['dll_sha256'][:16]}…  tool v{result['tool_version']}")
    for ver, d in result["firmwares"].items():
        print(f"\n=== firmware {ver}  (class {d['maps_class']}) ===")
        for name, e in d["fields"].items():
            extra = ""
            if "count" in e:
                extra = f"  [{e['record_size']}B x {e['count']} recs, span 0x{e['span_bytes']:X}]"
            elif "length_bytes" in e:
                extra = f"  [len {e['length_bytes']}]"
            elif "record_size" in e:
                extra = f"  [{e['record_size']}B records]"
            op = f" op={e.get('context_op')}" if e.get("context_op") else ""
            print(f"   {name:16} {e['addr']}{extra}{op}   <- {e['source']}  "
                  f"@{e['file_offset']} [{e['il_bytes']}]")
        print(f"   ({len(d['config_symbols_all'])} total getValue symbols, "
              f"{len(d['label_fields_all'])} label fields)")
    if args.validate:
        validate(result)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nwrote {args.json}")
    if args.full:
        for ver, d in result["firmwares"].items():
            print(f"\n----- firmware {ver} full EEPROM-region (0x14xxxxxx) symbols -----")
            cfg = sorted(((int(a, 16), n) for n, a in d["config_symbols_all"].items()
                          if a.startswith("0x14")), key=lambda x: x[0])
            for a, n in cfg:
                print(f"   0x{a:08X}  {n}")


if __name__ == "__main__":
    main()
