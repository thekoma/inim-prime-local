#!/usr/bin/env python3
"""Minimal ECMA-335 .NET metadata reader: PE -> CLI header -> #~ tables.
Provides TypeDef/MethodDef names and IL method-body bytes. Read-only, no exec.
"""
import struct

class PE:
    def __init__(self, data):
        self.data = data
        assert data[:2] == b"MZ"
        e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
        assert data[e_lfanew:e_lfanew+4] == b"PE\0\0"
        coff = e_lfanew + 4
        machine, self.nsec, _, _, _, opt_size, _ = struct.unpack_from("<HHIIIHH", data, coff)
        opt = coff + 20
        magic = struct.unpack_from("<H", data, opt)[0]
        self.pe32plus = (magic == 0x20B)
        # data directories: offset within optional header
        dd_off = opt + (112 if self.pe32plus else 96)
        ndd = struct.unpack_from("<I", data, opt + (108 if self.pe32plus else 92))[0]
        self.datadirs = [struct.unpack_from("<II", data, dd_off + 8*i) for i in range(ndd)]
        sec_off = opt + opt_size
        self.sections = []
        for i in range(self.nsec):
            name, vsize, vaddr, rawsize, rawptr = struct.unpack_from("<8sIIII", data, sec_off + 40*i)
            self.sections.append((name.rstrip(b"\0"), vaddr, vsize, rawptr, rawsize))

    def rva2off(self, rva):
        for name, vaddr, vsize, rawptr, rawsize in self.sections:
            if vaddr <= rva < vaddr + max(vsize, rawsize):
                return rawptr + (rva - vaddr)
        raise ValueError(f"RVA 0x{rva:X} not in any section")

# ECMA-335 table indices
T = dict(Module=0x00, TypeRef=0x01, TypeDef=0x02, Field=0x04, MethodDef=0x06,
         Param=0x08, InterfaceImpl=0x09, MemberRef=0x0A, Constant=0x0B,
         CustomAttribute=0x0C, FieldMarshal=0x0D, DeclSecurity=0x0E,
         ClassLayout=0x0F, FieldLayout=0x10, StandAloneSig=0x11, EventMap=0x12,
         Event=0x14, PropertyMap=0x15, Property=0x17, MethodSemantics=0x18,
         MethodImpl=0x19, ModuleRef=0x1A, TypeSpec=0x1B, ImplMap=0x1C,
         FieldRVA=0x1D, Assembly=0x20, AssemblyProcessor=0x21, AssemblyOS=0x22,
         AssemblyRef=0x23, AssemblyRefProcessor=0x24, AssemblyRefOS=0x25,
         File=0x26, ExportedType=0x27, ManifestResource=0x28, NestedClass=0x29,
         GenericParam=0x2A, MethodSpec=0x2B, GenericParamConstraint=0x2C)

class Meta:
    def __init__(self, data, pe):
        self.data = data; self.pe = pe
        cli_rva = pe.datadirs[14][0]
        cli = pe.rva2off(cli_rva)
        md_rva, md_size = struct.unpack_from("<II", data, cli+8)
        md = pe.rva2off(md_rva)
        assert data[md:md+4] == b"BSJB"
        ver_len = struct.unpack_from("<I", data, md+12)[0]
        p = md + 16 + ver_len
        p = (p + 3) & ~3  # align
        _flags, nstreams = struct.unpack_from("<HH", data, p)
        p += 4
        self.streams = {}
        for _ in range(nstreams):
            off, size = struct.unpack_from("<II", data, p); p += 8
            name = b""
            while data[p] != 0:
                name += bytes([data[p]]); p += 1
            p += 1
            p = (p + 3) & ~3
            self.streams[name.decode()] = (md + off, size)
        self._parse_tables()

    def _cstr(self, heap, idx):
        base, size = self.streams[heap]
        p = base + idx
        s = b""
        while self.data[p] != 0:
            s += bytes([self.data[p]]); p += 1
        return s.decode("utf-8", "replace")

    def strings(self, idx):
        return self._cstr("#Strings", idx)

    def _parse_tables(self):
        base, size = self.streams["#~"]
        d = self.data
        heap_sizes = d[base+6]
        self.str_wide = bool(heap_sizes & 1)
        self.guid_wide = bool(heap_sizes & 2)
        self.blob_wide = bool(heap_sizes & 4)
        valid = struct.unpack_from("<Q", d, base+8)[0]
        self.rows = {}
        present = [i for i in range(64) if (valid >> i) & 1]
        p = base + 24
        for i in present:
            self.rows[i] = struct.unpack_from("<I", d, p)[0]; p += 4
        # p now at start of table rows
        self._row_schemas()
        self.table_off = {}
        self.table_rowsize = {}
        for i in range(64):
            if i in self.rows:
                self.table_off[i] = p
                rs = self._rowsize(i)
                self.table_rowsize[i] = rs
                p += rs * self.rows[i]

    def _idx_size(self, table):
        return 4 if self.rows.get(table, 0) >= 65536 else 2

    def _coded_size(self, tag_tables, tag_bits):
        maxrows = max((self.rows.get(t, 0) for t in tag_tables), default=0)
        return 4 if maxrows >= (1 << (16 - tag_bits)) else 2

    def _row_schemas(self):
        S = self.str_wide and 4 or 2
        G = self.guid_wide and 4 or 2
        B = self.blob_wide and 4 or 2
        def ti(t): return self._idx_size(t)
        # coded index groups
        def TypeDefOrRef(): return self._coded_size([T['TypeDef'],T['TypeRef'],T['TypeSpec']], 2)
        def HasConstant(): return self._coded_size([T['Field'],T['Param'],T['Property']], 2)
        def HasCustomAttribute(): return self._coded_size([T['MethodDef'],T['Field'],T['TypeRef'],T['TypeDef'],
            T['Param'],T['InterfaceImpl'],T['MemberRef'],T['Module'],T['DeclSecurity'],T['Property'],T['Event'],
            T['StandAloneSig'],T['ModuleRef'],T['TypeSpec'],T['Assembly'],T['AssemblyRef'],T['File'],
            T['ExportedType'],T['ManifestResource'],T['GenericParam'],T['GenericParamConstraint'],T['MethodSpec']], 5)
        def HasFieldMarshal(): return self._coded_size([T['Field'],T['Param']], 1)
        def HasDeclSecurity(): return self._coded_size([T['TypeDef'],T['MethodDef'],T['Assembly']], 2)
        def MemberRefParent(): return self._coded_size([T['TypeDef'],T['TypeRef'],T['ModuleRef'],T['MethodDef'],T['TypeSpec']], 3)
        def HasSemantics(): return self._coded_size([T['Event'],T['Property']], 1)
        def MethodDefOrRef(): return self._coded_size([T['MethodDef'],T['MemberRef']], 1)
        def MemberForwarded(): return self._coded_size([T['Field'],T['MethodDef']], 1)
        def Implementation(): return self._coded_size([T['File'],T['AssemblyRef'],T['ExportedType']], 2)
        def CustomAttributeType(): return self._coded_size([T['MethodDef'],T['MemberRef']], 3)  # with 2 unused -> 5 tables worth but 3 bits
        def ResolutionScope(): return self._coded_size([T['Module'],T['ModuleRef'],T['AssemblyRef'],T['TypeRef']], 2)
        def TypeOrMethodDef(): return self._coded_size([T['TypeDef'],T['MethodDef']], 1)
        self._schema = {
            0x00: [2, S, G, G, G],                                  # Module
            0x01: [ResolutionScope, S, S],                          # TypeRef
            0x02: [4, S, S, TypeDefOrRef, ti(T['Field']), ti(T['MethodDef'])],  # TypeDef
            0x04: [2, S, B],                                        # Field
            0x06: [4, 2, 2, S, B, ti(T['Param'])],                 # MethodDef: RVA, ImplFlags, Flags, Name, Sig, ParamList
            0x08: [2, 2, S],                                        # Param
            0x09: [ti(T['TypeDef']), TypeDefOrRef],                 # InterfaceImpl
            0x0A: [MemberRefParent, S, B],                          # MemberRef
            0x0B: [1, 1, HasConstant, B],                           # Constant (type is 1 byte + 1 pad)
            0x0C: [HasCustomAttribute, CustomAttributeType, B],     # CustomAttribute
            0x0D: [HasFieldMarshal, B],                             # FieldMarshal
            0x0E: [2, HasDeclSecurity, B],                          # DeclSecurity
            0x0F: [2, 4, ti(T['TypeDef'])],                         # ClassLayout
            0x10: [4, ti(T['Field'])],                              # FieldLayout
            0x11: [B],                                              # StandAloneSig
            0x12: [ti(T['TypeDef']), ti(T['Event'])],              # EventMap
            0x14: [2, S, TypeDefOrRef],                             # Event
            0x15: [ti(T['TypeDef']), ti(T['Property'])],           # PropertyMap
            0x17: [2, S, B],                                        # Property
            0x18: [2, MethodDefOrRef, HasSemantics],               # MethodSemantics
            0x19: [ti(T['TypeDef']), MethodDefOrRef, MethodDefOrRef],  # MethodImpl
            0x1A: [S],                                              # ModuleRef
            0x1B: [B],                                              # TypeSpec
            0x1C: [2, MemberForwarded, S, ti(T['ModuleRef'])],     # ImplMap
            0x1D: [4, ti(T['Field'])],                              # FieldRVA
            0x20: [4, 2, 2, 2, 2, 2, B, S, S],                     # Assembly
            0x21: [4],                                              # AssemblyProcessor
            0x22: [4, 4, 4],                                        # AssemblyOS
            0x23: [2, 2, 2, 2, 4, B, S, S, B],                     # AssemblyRef
            0x24: [4, ti(T['AssemblyRef'])],                       # AssemblyRefProcessor
            0x25: [4, 4, 4, ti(T['AssemblyRef'])],                 # AssemblyRefOS
            0x26: [4, S, B],                                        # File
            0x27: [4, 4, S, S, Implementation],                    # ExportedType
            0x28: [4, 4, S, Implementation],                       # ManifestResource
            0x29: [ti(T['TypeDef']), ti(T['TypeDef'])],            # NestedClass
            0x2A: [2, 2, TypeOrMethodDef, S],                      # GenericParam
            0x2B: [MethodDefOrRef, B],                             # MethodSpec
            0x2C: [ti(T['GenericParam']), TypeDefOrRef],           # GenericParamConstraint
        }

    def _rowsize(self, tbl):
        total = 0
        for c in self._schema[tbl]:
            total += c() if callable(c) else c
        return total

    def _read_col(self, p, width):
        if width == 2:
            return struct.unpack_from("<H", self.data, p)[0]
        return struct.unpack_from("<I", self.data, p)[0]

    def row(self, tbl, idx1):  # 1-based row index
        base = self.table_off[tbl]
        rs = self.table_rowsize[tbl]
        p = base + (idx1-1)*rs
        out = []
        for c in self._schema[tbl]:
            w = c() if callable(c) else c
            out.append(self._read_col(p, w))
            p += w
        return out
