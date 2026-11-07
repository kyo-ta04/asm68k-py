#!/usr/bin/env python3
"""asm68k -- MC68000 assembler (prototype) with C-source output.

Assembles a 68000 assembly source file through the prototype
pipeline (pass 0 / 0.5 / 1 / 2 over the tape deck) and writes the
resulting image as a C source array in the style of ehbasic.c:

    const uint8_t NAME[] = {
    // 0x0000
      0x00, 0x00, ... // 00
    };
    const int name_size = sizeof(NAME);

Usage:
    asm68k.py SOURCE.asm [-o OUT.c] [--name NAME] [--bin OUT.bin]
              [--list OUT.lst]
    asm68k.py SOURCE.asm --check TARGET.c [--array NAME] [-o OUT.c]

--bin writes the raw binary image: exactly the bytes of the C array
(materialised span, gaps filled with $FF, no header).

--list writes an assembly listing: address + emitted bytes beside the
original source lines (comments included, original line numbers,
INCLUDEs expanded in place), with EQU values shown and a symbol
table at the end. "-" writes the listing to stdout.

INCLUDE lines are resolved relative to the including file.
Exit status: 0 = ok (and identical, when --check is given),
1 = check mismatch, 2 = assembly error.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tapedeck as td
import pass0 as P0
import pass05 as P05
import pass12 as P12


SLOT_DISPLAY: dict[int, str] = {}


def register_includes(deck: td.TapeDeck, path: str, slot_counter: list[int],
                      loaded: set[str] | None = None) -> None:
    """Load INCLUDEd files (recursively) into free tape slots."""
    if loaded is None:
        loaded = set()
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return
    text = raw.decode("latin-1")
    base = os.path.dirname(os.path.abspath(path))
    for m in re.finditer(r'INCLUDE\s+"?([^"\s;]+)"?', text, re.I):
        fname = m.group(1)
        key = os.path.basename(fname).upper()
        if key in loaded:
            continue
        loaded.add(key)
        inc_path = os.path.join(base, fname)
        if not os.path.exists(inc_path):
            inc_path = os.path.join(base, os.path.basename(fname))
        if not os.path.exists(inc_path):
            continue
        slot = slot_counter[0]
        slot_counter[0] += 1
        deck.load_file(slot, inc_path)
        P0.INCLUDE_MAP[key] = slot
        SLOT_DISPLAY[slot] = fname
        register_includes(deck, inc_path, slot_counter, loaded)


def assemble(path: str, want_listing: bool = False):
    """-> (image dict, errors, unresolved names, listing info).

    listing info is None unless want_listing: then a dict with the
    pass-0 formatted lines, per-record listing entries from pass 2,
    EQU line values, and the symbol table data. Raises on hard errors.
    """
    deck = td.TapeDeck()
    deck.load_file(P0.SLOT_ASM, path)
    register_includes(deck, path, [10])
    deck.select(P0.SLOT_FORMATTED)
    deck.new_tape()
    p0 = P0.Pass0(deck, P0.SLOT_FORMATTED)
    p0.process_slot(P0.SLOT_ASM, 0)
    if p0.st.equ_other:
        pass  # INCLUDE misses etc. are tolerated if nothing else breaks
    p5 = P05.Pass05(deck)
    lines = p5.read_lines()
    p5.run(lines)
    intermediate = deck.dump_bytes(P05.SLOT_INTERMEDIATE)
    mains = P12.parse_records(intermediate, p5.opcodes)
    values = [None] * (len(p5.symbols.names) + 1)
    ev = P12.Eval(values)
    decisions, errors = {}, []
    prev = None
    for _ in range(12):
        decisions = {r["idx"]: {} for r in mains}
        errors = []
        P12.pass1(mains, ev, values, decisions, errors)
        snap = (tuple(values),
                tuple(sorted((k, tuple(sorted(v.items())))
                             for k, v in decisions.items())))
        if snap == prev:
            break
        prev = snap
    entries = [] if want_listing else None
    image, _end = P12.pass2(mains, ev, values, decisions, errors,
                            listing=entries)
    unresolved = [p5.symbols.names[i - 1] for i in range(1, len(values))
                  if values[i] is None]
    info = None
    if want_listing:
        equ = {}
        for r in mains:
            if r["opcode"] == "EQU" and r["label"]:
                equ[r["line"]] = (p5.symbols.names[r["label"] - 1],
                                  values[r["label"]])
        slot_names = {P0.SLOT_ASM: os.path.basename(path)}
        for inc_name, inc_slot in P0.INCLUDE_MAP.items():
            slot_names.setdefault(inc_slot, inc_name)
        slot_names.update(SLOT_DISPLAY)
        origins = [(slot_names.get(slot, f"slot{slot}"), lineno, raw)
                   for slot, lineno, raw in p0.origins]
        source_map = [(slot_names.get(rec[0], f"slot{rec[0]}"),
                       rec[1], rec[2], rec[3]) for rec in p0.source_map]
        info = {"lines": lines, "entries": entries, "equ": equ,
                "origins": origins, "source_map": source_map,
                "names": p5.symbols.names, "kinds": p5.symbols.kind,
                "values": values}
    return image, errors, unresolved, info


def materialize(image: dict[int, int]):
    lo, hi = min(image), max(image)
    return lo, bytes(image.get(a, 0xFF) for a in range(lo, hi + 1))


def to_c(name: str, base: int, data: bytes, srcname: str) -> str:
    size_name = re.sub(r"[^A-Za-z0-9_]", "_", name).lower() + "_size"
    out = [f"// {name} - 68000 program image (generated by asm68k from {srcname})",
           '#include <stdint.h>', "",
           f"const uint8_t {name}[] = {{"]
    for off in range(0, len(data), 16):
        addr = base + off
        if addr % 256 == 0:
            out.append("")
            out.append(f"// 0x{addr:04x}")
        row = data[off:off + 16]
        comma = "," if off + 16 < len(data) else ""
        out.append("  " + ", ".join(f"0x{b:02x}" for b in row) + comma
                   + f"  // {addr & 0xFF:02x}")
    out.append("};")
    out.append(f"const int {size_name} = sizeof({name});")
    return "\n".join(out) + "\n"


def to_listing(srcname: str, info: dict) -> str:
    lines, entries = info["lines"], info["entries"]
    smap = info.get("source_map") or []
    emitted = sum(1 for rec in smap if rec[3] is not None)
    use_map = emitted == len(entries) and len(smap) >= len(entries)
    out = [f"; asm68k listing: {srcname}",
           "; every source line is shown in order (INCLUDEs expanded",
           "; in place); lines that produce no code appear without",
           "; address/bytes. Files are marked with ===== separators.",
           ""]
    if use_map:
        rows = [(fname, srcline, raw,
                 entries[fmt_idx] if fmt_idx is not None else None)
                for fname, srcline, raw, fmt_idx in smap]
    else:  # fallback: one row per formatted line, formatted text
        rows = [(None, None,
                 lines[ent["line"] - 1] if 1 <= ent["line"] <= len(lines)
                 else "", ent)
                for ent in entries]
    cur_file = None
    for fname, srcline, raw, ent in rows:
        if fname is not None and fname != cur_file:
            cur_file = fname
            out.append(f"; ===== {fname} =====")
        text = raw.rstrip()
        num = f"{srcline:>5}" if srcline is not None else " " * 5
        if ent is None:
            out.append(f"{' ' * 6}  {'':<24}  {num}  {text}".rstrip())
            continue
        ln = ent["line"]
        addr = f"{ent['addr']:06X}" if ent["addr"] is not None else " " * 6
        data = ent["data"]
        if data is None and ln in info["equ"]:
            _nm, val = info["equ"][ln]
            cell = f"= {val:08X}" if val is not None else "= ?"
            out.append(f"{addr}  {cell:<24}  {num}  {text}")
        elif data:
            for off in range(0, len(data), 8):
                cell = " ".join(f"{b:02X}" for b in data[off:off + 8])
                row_addr = f"{ent['addr'] + off:06X}" if off == 0 else " " * 6
                out.append(f"{row_addr}  {cell:<24}  "
                           f"{num if off == 0 else ' ' * 5}  "
                           f"{text if off == 0 else ''}".rstrip())
        elif ent["reserve"]:
            cell = f"({ent['reserve']} bytes reserved)"
            out.append(f"{addr}  {cell:<24}  {num}  {text}")
        else:
            out.append(f"{addr}  {'':<24}  {num}  {text}".rstrip())
    names, kinds, values = info["names"], info["kinds"], info["values"]
    out += ["", f"; symbol table ({len(names)} symbols)", ""]
    kind_name = {"E": "EQU", "L": "label", "U": "undef"}
    for sid in sorted(range(1, len(names) + 1), key=lambda s: names[s - 1]):
        v = values[sid]
        vtxt = f"{v:08X}" if v is not None else "--------"
        out.append(f"{names[sid - 1]:<24} {vtxt}  "
                   f"{kind_name.get(kinds.get(sid, '?'), '?')}")
    return "\n".join(out) + "\n"


def extract_c_bytes(path: str, array: str | None) -> bytes:
    with open(path) as f:
        text = f.read()
    if array:
        m = re.search(rf"{re.escape(array)}\[\] = \{{(.*?)\}};", text, re.S)
        if not m:
            raise SystemExit(f"array {array} not found in {path}")
        text = m.group(1)
    data = []
    for ln in text.splitlines():
        code = ln.split("//")[0]
        data += [int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{2})", code)]
    return bytes(data)


def main() -> int:
    ap = argparse.ArgumentParser(prog="asm68k", description=__doc__)
    ap.add_argument("source", help="assembly source file (.asm)")
    ap.add_argument("-o", "--output", help="output C file (default: <source>.c)")
    ap.add_argument("--name", help="C array name (default: source basename)")
    ap.add_argument("--check", metavar="TARGET.c",
                    help="compare the image against a C array file")
    ap.add_argument("--array", help="array name to compare in --check target")
    ap.add_argument("--bin", metavar="OUT.bin",
                    help="also write the raw binary image "
                         "(same span as the C array)")
    ap.add_argument("--list", metavar="OUT.lst", dest="listing",
                    help="also write an assembly listing "
                         "(address + bytes + source, symbol table); "
                         "'-' for stdout")
    args = ap.parse_args()

    image, errors, unresolved, info = assemble(
        args.source, want_listing=bool(args.listing))
    if errors or unresolved:
        for e in errors:
            print(f"asm68k: error: {e}", file=sys.stderr)
        for u in unresolved:
            print(f"asm68k: error: unresolved symbol {u}", file=sys.stderr)
        return 2
    base, data = materialize(image)
    name = args.name or re.sub(r"[^A-Za-z0-9_]", "_",
                               os.path.splitext(os.path.basename(args.source))[0])
    out_path = args.output or (os.path.splitext(os.path.basename(args.source))[0] + ".c")
    with open(out_path, "w") as f:
        f.write(to_c(name, base, data, os.path.basename(args.source)))
    print(f"asm68k: {args.source} -> {out_path}: {len(data)} bytes "
          f"(${base:04x}-${base + len(data) - 1:04x}), array {name}")
    if args.bin:
        with open(args.bin, "wb") as f:
            f.write(data)
        print(f"asm68k: binary -> {args.bin}: {len(data)} bytes "
              f"(load at ${base:04x})")
    if args.listing:
        text = to_listing(os.path.basename(args.source), info)
        if args.listing == "-":
            print(text, end="")
        else:
            # latin-1: source lines are carried as latin-1 text, so the
            # listing keeps the original bytes (e.g. in comments)
            with open(args.listing, "w", encoding="latin-1") as f:
                f.write(text)
            print(f"asm68k: listing -> {args.listing}: "
                  f"{len(info['entries'])} lines, "
                  f"{len(info['names'])} symbols")
    if args.check:
        target = extract_c_bytes(args.check, args.array)
        n = max(len(target), len(data))
        mism = [i for i in range(n)
                if (data[i] if i < len(data) else 0x00)
                != (target[i] if i < len(target) else 0x00)]
        if mism:
            print(f"asm68k: check FAILED: {len(mism)} mismatched bytes "
                  f"(first at ${base + mism[0]:04x})")
            return 1
        print(f"asm68k: check OK: identical ({len(target)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
