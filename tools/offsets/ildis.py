#!/usr/bin/env python3
"""Tiny IL disassembler for selected methods (enough to read getValue switches)."""
import sys, struct
from dotnetmeta import PE, Meta, T

# single-byte opcodes we care to name; others printed as .byte
OPS = {
 0x00:"nop",0x01:"break",0x02:"ldarg.0",0x03:"ldarg.1",0x04:"ldarg.2",0x05:"ldarg.3",
 0x06:"ldloc.0",0x07:"ldloc.1",0x08:"ldloc.2",0x09:"ldloc.3",0x0a:"stloc.0",0x0b:"stloc.1",
 0x0c:"stloc.2",0x0d:"stloc.3",0x0e:("ldarg.s",1),0x0f:("ldarga.s",1),0x10:("starg.s",1),
 0x11:("ldloc.s",1),0x12:("ldloca.s",1),0x13:("stloc.s",1),0x14:"ldnull",0x15:"ldc.i4.m1",
 0x16:"ldc.i4.0",0x17:"ldc.i4.1",0x18:"ldc.i4.2",0x19:"ldc.i4.3",0x1a:"ldc.i4.4",0x1b:"ldc.i4.5",
 0x1c:"ldc.i4.6",0x1d:"ldc.i4.7",0x1e:"ldc.i4.8",0x1f:("ldc.i4.s",1),0x20:("ldc.i4",4),
 0x21:("ldc.i8",8),0x22:("ldc.r4",4),0x23:("ldc.r8",8),0x25:"dup",0x26:"pop",
 0x28:("call",4),0x29:("calli",4),0x2a:"ret",
 0x2b:("br.s",1),0x2c:("brfalse.s",1),0x2d:("brtrue.s",1),0x2e:("beq.s",1),0x2f:("bge.s",1),
 0x30:("bgt.s",1),0x31:("ble.s",1),0x32:("blt.s",1),0x33:("bne.un.s",1),0x34:("bge.un.s",1),
 0x35:("bgt.un.s",1),0x36:("ble.un.s",1),0x37:("blt.un.s",1),0x38:("br",4),0x39:("brfalse",4),
 0x3a:("brtrue",4),0x3b:("beq",4),0x3c:("bge",4),0x3d:("bgt",4),0x3e:("ble",4),0x3f:("blt",4),
 0x40:("bne.un",4),0x45:("switch",-1),0x58:"add",0x59:"sub",0x5a:"mul",0x5b:"div",
 0x6f:("callvirt",4),0x72:("ldstr",4),0x8d:("newarr",4),0x7b:("ldfld",4),0x7d:("stfld",4),
 0x7e:("ldsfld",4),0x80:("stsfld",4),0xfe:"PREFIX",
}

# Operand byte-size of every single-byte opcode (for exact linear decoding).
# Default 0 for stack/arith ops not listed above; special cases carry operands.
_SINGLE_OPERAND = {
 0x0e:1,0x0f:1,0x10:1,0x11:1,0x12:1,0x13:1,0x1f:1,0x20:4,0x21:8,0x22:4,0x23:8,
 0x27:4,0x28:4,0x29:4,0x2b:1,0x2c:1,0x2d:1,0x2e:1,0x2f:1,0x30:1,0x31:1,0x32:1,
 0x33:1,0x34:1,0x35:1,0x36:1,0x37:1,0x38:4,0x39:4,0x3a:4,0x3b:4,0x3c:4,0x3d:4,
 0x3e:4,0x3f:4,0x40:4,0x45:-1,0x6f:4,0x70:4,0x71:4,0x72:4,0x73:4,0x74:4,0x75:4,
 0x79:4,0x7b:4,0x7c:4,0x7d:4,0x7e:4,0x7f:4,0x80:4,0x81:4,0x8c:4,0x8d:4,0x8f:4,
 0xa3:4,0xa4:4,0xa5:4,0xc2:4,0xc6:4,0x28:4,0xd0:4,0xfe:0,
}
# Operand byte-size of FE-prefixed two-byte opcodes.
_FE_OPERAND = {
 0x06:4,0x07:4,0x09:2,0x0a:2,0x0b:2,0x0c:2,0x0d:2,0x0e:2,0x15:4,0x16:4,
 0x18:0,0x1c:4,0x1d:0,0x1e:0,0x20:4,0x21:0,0x22:0,
}

def load(path):
    data = open(path,"rb").read()
    pe = PE(data); m = Meta(data,pe)
    return data, pe, m

def us_string(m, tok):
    """Resolve a ldstr token (0x70xxxxxx) to its string via the #US heap."""
    if not isinstance(tok, int) or (tok >> 24) != 0x70:
        return None
    idx = tok & 0xFFFFFF
    base, size = m.streams["#US"]
    p = base + idx; d = m.data
    b = d[p]
    if b & 0x80 == 0:
        ln = b; p += 1
    elif b & 0xC0 == 0x80:
        ln = ((b & 0x3F) << 8) | d[p+1]; p += 2
    else:
        ln = ((b & 0x1F) << 24) | (d[p+1] << 16) | (d[p+2] << 8) | d[p+3]; p += 4
    raw = d[p:p+ln]
    if ln % 2 == 1:
        raw = raw[:-1]
    return raw.decode("utf-16-le", "replace")

def find_method(m, typ, meth):
    n_type = m.rows[T['TypeDef']]
    for i in range(1, n_type+1):
        r = m.row(T['TypeDef'], i)
        name = m.strings(r[1]); ns = m.strings(r[2])
        if name != typ: continue
        first = r[5]
        last = m.row(T['TypeDef'], i+1)[5] if i < n_type else m.rows[T['MethodDef']]+1
        for mi in range(first, last):
            mr = m.row(T['MethodDef'], mi)
            if m.strings(mr[3]) == meth:
                return mi, mr
    return None, None

def body_info(data, pe, rva):
    """Return (il_bytes, body_file_start) so IL offsets map to absolute file offsets."""
    off = pe.rva2off(rva)
    b0 = data[off]
    if (b0 & 3) == 2:  # tiny header
        size = b0 >> 2
        start = off + 1
        return data[start:start+size], start
    flags_size = struct.unpack_from("<H", data, off)[0]
    hdr = (flags_size >> 12) * 4
    code_size = struct.unpack_from("<I", data, off+4)[0]
    start = off + hdr
    return data[start:start+code_size], start

def body(data, pe, rva):
    return body_info(data, pe, rva)[0]

def disasm(il, m):
    p = 0; out = []
    while p < len(il):
        start = p; op = il[p]; p += 1
        info = OPS.get(op)
        if info == "PREFIX":
            op2 = il[p]; p += 1
            p += _FE_OPERAND.get(op2, 0)   # consume operand to stay in sync
            out.append((start, f"fe{op2:02x}", None)); continue
        if info is None:
            p += _SINGLE_OPERAND.get(op, 0)  # consume operand to stay in sync
            out.append((start, f".byte 0x{op:02x}", None)); continue
        if isinstance(info, str):
            out.append((start, info, None)); continue
        name, n = info
        if n == -1:  # switch
            cnt = struct.unpack_from("<I", il, p)[0]; p += 4
            targets = [struct.unpack_from("<i", il, p+4*k)[0] for k in range(cnt)]
            p += 4*cnt
            out.append((start, f"switch[{cnt}]", targets)); continue
        raw = il[p:p+n]; p += n
        if name in ("ldc.i4",):
            val = struct.unpack_from("<i", raw, 0)[0]
            out.append((start, f"ldc.i4 0x{val & 0xffffffff:08X}", val)); continue
        if name in ("ldc.i4.s","ldarg.s","ldloc.s","stloc.s","ldloca.s","starg.s","ldarga.s"):
            out.append((start, f"{name} {raw[0]}", raw[0])); continue
        if name in ("call","callvirt","ldstr","newarr","ldfld","stfld","ldsfld","stsfld"):
            tok = struct.unpack_from("<I", raw,0)[0]
            extra = ""
            if name == "ldstr" and (tok>>24)==0x70:
                pass
            out.append((start, f"{name} 0x{tok:08X}{extra}", tok)); continue
        # branches
        if n in (1,4):
            disp = struct.unpack_from("<b" if n==1 else "<i", raw,0)[0]
            tgt = p + disp
            out.append((start, f"{name} -> 0x{tgt:04x}", tgt)); continue
        out.append((start, name, None))
    return out

if __name__ == "__main__":
    path, typ, meth = sys.argv[1], sys.argv[2], sys.argv[3]
    data, pe, m = load(path)
    mi, mr = find_method(m, typ, meth)
    if mi is None:
        print("method not found"); sys.exit(1)
    il = body(data, pe, mr[0])
    print(f"{typ}::{meth} RVA=0x{mr[0]:X} IL len={len(il)}")
    for start, text, val in disasm(il, m):
        print(f"  IL_{start:04x}: {text}")
