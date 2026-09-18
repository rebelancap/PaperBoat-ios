#!/usr/bin/env python3
"""
pack-crc-match.py — map MasterKillua GLideN64 pack PNGs onto PaperBoat asset
paths by recomputing the SAME Rice CRC GLideN64 used to name them.

Adapted from Lighthouse-ios/scripts/pack-crc-match.py. Two PaperBoat-specific
differences, both documented in docs/texture-pack-hts.md:

 1. The MasterKillua .hts files are OLD-version caches: they store no
    n64 fmt/siz, so the decoded PNGs are named
    `<rom>#<texCRC>#<palCRC|none>_all.png` and the key is (texCRC, palCRC).
 2. pm64.o2r is palette-heavy (8,752 of 13,908 texture records are CI4/CI8),
    so the TLUT CRC path is the MAJORITY path. PM64 stores palettes as
    separate RGBA16 Texture records named `<tex>_pal<N>` / `<tex>_pal_<N>`.

CRC transcription: GLideN64 src/GLideNHQ/TxUtil.cpp (RiceCRC32, CalculateMaxCI4b/8b,
checksum64), non-ASM reference path. The 32-bit texel read is BIG-ENDIAN —
Torch stores the N64's own big-endian texels (Lighthouse M-006).

Usage:
    scripts/pack-crc-match.py --archive oracle/shiphome/pm64.o2r \
        --pack work/packs/masterkillua-hd [--report work/packs/match.json]
"""

import argparse
import collections
import json
import re
import struct
import sys
import zipfile
from pathlib import Path

OTR_HEADER_SIZE = 64
RESTYPE_TEXTURE = 0x4F544558
M32 = 0xFFFFFFFF

# Fast::TextureType -> (n64 fmt, n64 siz). fmt: 0=RGBA 2=CI 3=IA 4=I
TEXTYPE = {
    1: (0, 3),   # RGBA32bpp
    2: (0, 2),   # RGBA16bpp
    3: (2, 0),   # Palette4bpp  (CI4)
    4: (2, 1),   # Palette8bpp  (CI8)
    5: (4, 0),   # Grayscale4bpp       I4
    6: (4, 1),   # Grayscale8bpp       I8
    7: (3, 0),   # GrayscaleAlpha4bpp  IA4
    8: (3, 1),   # GrayscaleAlpha8bpp  IA8
    9: (3, 2),   # GrayscaleAlpha16bpp IA16
}

# PM64 stores TLUTs as separate RGBA16 Texture records, under FOUR naming
# conventions (all four verified against pm64.o2r):
#   textures/<t>_tlut            — world/map textures
#   <t>.pal                      — icons/, ui/, battle/, effects/, …
#   <t>_pal0 / <t>_pal_0         — backgrounds/ (one palette per texture)
#   <sprite>_pal_<j>             — sprites/: a SHARED palette set for every
#                                  <sprite>_raster_<i> of that sprite
#   charset/<set>_palette_<j>    — charset/: shared set for <set>_<i>
# So a texture's candidate palettes come from its own name AND from its group
# prefix (raster/charset index stripped).
PAL_SUFFIXES = [
    re.compile(r"_palette_\d+$"),
    re.compile(r"_pal_?\d+$"),
    re.compile(r"_tlut$"),
    re.compile(r"\.pal$"),
]
RASTER_RE = re.compile(r"_raster_\d+$")
TRAILNUM_RE = re.compile(r"_\d+$")


def palette_base(name: str):
    for s in PAL_SUFFIXES:
        m = s.search(name)
        if m:
            return name[: m.start()]
    return None


def texture_bases(name: str):
    out = [name]
    m = RASTER_RE.search(name)
    if m:
        out.append(name[: m.start()])
    else:
        m2 = TRAILNUM_RE.search(name)
        if m2:
            out.append(name[: m2.start()])
    return out


def rice_crc32(buf: bytes, width: int, height: int, size: int, row_stride: int):
    """Verbatim transcription of TxUtil::RiceCRC32 (non-ASM path)."""
    bytes_per_line = (width << size) >> 1
    if bytes_per_line < 4 or height < 1:
        return None
    crc = 0
    base = 0
    y = height - 1
    unpack = struct.unpack_from
    while True:
        esi = 0
        x = bytes_per_line - 4
        while True:
            off = base + x
            if off < 0 or off + 4 > len(buf):
                return None
            esi = (unpack(">I", buf, off)[0] ^ (x & M32)) & M32
            crc = (((crc << 4) & M32) + ((crc >> 28) & 15) + esi) & M32
            x -= 4
            if x < 0:
                break
        esi = (esi ^ (y & M32)) & M32
        crc = (crc + esi) & M32
        base += row_stride
        y -= 1
        if y < 0:
            break
    return crc


def cimax_ci8(buf, width, height, row_stride):
    val = 0
    for y in range(height):
        row = buf[row_stride * y: row_stride * y + width]
        if not row:
            continue
        m = max(row)
        if m > val:
            val = m
        if val == 0xFF:
            return 0xFF
    return val


def cimax_ci4(buf, width, height, row_stride):
    val = 0
    w = width >> 1
    for y in range(height):
        row = buf[row_stride * y: row_stride * y + w]
        for b in row:
            v1, v2 = b >> 4, b & 0xF
            if v1 > val:
                val = v1
            if v2 > val:
                val = v2
            if val == 0xF:
                return 0xF
    return val


def read_texture(raw: bytes):
    """Parse a PM64/LUS binary Texture record -> (type, w, h, flags, texels)."""
    if len(raw) < OTR_HEADER_SIZE + 16:
        return None
    if struct.unpack_from("<I", raw, 4)[0] != RESTYPE_TEXTURE:
        return None
    ver = struct.unpack_from("<I", raw, 8)[0]
    o = OTR_HEADER_SIZE
    if ver == 0:                       # PM64::ResourceFactoryBinaryTextureV0
        ttype, w, h, sz = struct.unpack_from("<IIII", raw, o)
        data = raw[o + 16:]
    elif ver == 1:                     # Fast::ResourceFactoryBinaryTextureV1
        ttype, w, h, flags, hs, vs, sz = struct.unpack_from("<IIIIffI", raw, o)
        data = raw[o + 28:]
    else:
        return None
    return ttype, w, h, (data[:sz] if sz and sz <= len(data) else data)


def category(path: str) -> str:
    top = path.split("/", 1)[0]
    if top in ("sprites", "entities", "party", "effects", "sprite_shading"):
        return "sprite"
    if top in ("ui", "icons", "charset", "messages", "level_up", "battle",
               "title_screen", "logos", "story_images", "theater"):
        return "ui"
    if top in ("textures", "backgrounds", "shapes", "world", "misc", "imgfx"):
        return "map"
    return "other"


def load_archive(path: Path):
    z = zipfile.ZipFile(path)
    tex, pals = {}, collections.defaultdict(list)
    for n in z.namelist():
        try:
            raw = z.read(n)
        except Exception:
            continue
        r = read_texture(raw)
        if not r:
            continue
        tex[n] = r
        # A palette record is RGBA16, one row, 16 or 256 entries.
        ttype, w, h, _d = r
        if ttype == 2 and h == 1 and w in (16, 256):
            b = palette_base(n)
            if b is not None:
                pals[b].append(n)
    return tex, pals


def load_pack(pack_dir: Path):
    """index.json -> {(texCRC, palCRC): entry}, plus a texCRC->set(palCRC) map."""
    idx = json.loads((pack_dir / "index.json").read_text())
    pack, by_tex = {}, collections.defaultdict(set)
    for e in idx["entries"]:
        t = int(e["tex_crc"], 16)
        p = 0 if e["pal_crc"] in (None, "none") else int(e["pal_crc"], 16)
        pack[(t, p)] = e
        by_tex[t].add(p)
    return pack, by_tex


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", required=True, type=Path)
    ap.add_argument("--pack", required=True, type=Path)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()

    pack, pack_by_tex = load_pack(args.pack)
    print(f"pack entries: {len(pack)}  distinct texCRCs: {len(pack_by_tex)}", file=sys.stderr)

    tex, pals = load_archive(args.archive)
    print(f"archive texture records: {len(tex)}  palette groups: {len(pals)}", file=sys.stderr)

    # ---- palette CRC indices ------------------------------------------------
    # The TLUT CRC is RiceCRC32(palette, cimax+1, 1, size=2, stride 32|512),
    # so it depends on the texture's cimax as well as the palette bytes. Two
    # lookups are built:
    #   pal_cache  — (palette, siz, cimax) -> crc, for the NAMED association
    #   global_idx — (siz, cimax) -> {crc: [palette names]}, over EVERY palette
    #                record in the archive.
    # The global index is what makes the match honest rather than lucky: a
    # (texCRC, palCRC) pair only exists in the pack because GLideN64 saw that
    # exact texel+TLUT combination in real gameplay, so finding both halves
    # anywhere in the archive identifies the pair. PM64 shares TLUTs far more
    # widely than its file naming suggests (sprite palette sets, charset sets,
    # and plain reuse across unrelated assets).
    pal_cache = {}
    pal_records = {n: r for n, r in tex.items() if r[0] == 2 and r[2] == 1 and r[1] in (16, 256)}
    global_idx = {}

    def global_palettes(siz, cimax):
        key = (siz, cimax)
        idx = global_idx.get(key)
        if idx is not None:
            return idx
        idx = collections.defaultdict(list)
        want_w = 16 if siz == 0 else 256
        stride = 32 if siz == 0 else 512
        for pn, (_t, pw, _ph, pdata) in pal_records.items():
            if pw != want_w:
                continue
            c = rice_crc32(pdata, cimax + 1, 1, 2, stride)
            if c is not None:
                idx[c].append(pn)
        global_idx[key] = idx
        return idx

    def palette_crc(pal_name, siz, cimax):
        key = (pal_name, siz, cimax)
        if key in pal_cache:
            return pal_cache[key]
        r = tex.get(pal_name)
        if not r:
            pal_cache[key] = None
            return None
        _, _, _, pdata = r
        stride = 32 if siz == 0 else 512
        crc = rice_crc32(pdata, cimax + 1, 1, 2, stride)
        pal_cache[key] = crc
        return crc

    stats = collections.Counter()
    cat_tot, cat_hit = collections.Counter(), collections.Counter()
    matched = {}            # asset path -> {"png":…, "pal":…, "texcrc":…, "palcrc":…}
    texcrc_only = {}        # asset path -> texCRC present in pack under some other palette
    for name, (ttype, w, h, data) in sorted(tex.items()):
        if ttype not in TEXTYPE:
            stats["unknown-type"] += 1
            continue
        fmt, siz = TEXTYPE[ttype]
        row_stride = (w << siz) >> 1
        crc = rice_crc32(data, w, h, siz, row_stride)
        if crc is None:
            stats["crc-unavailable"] += 1
            continue
        is_pal = ttype == 2 and h == 1 and w in (16, 256)
        if is_pal:
            stats["palette-record"] += 1
            continue   # TLUTs are not replaceable art; an RGBA pack supersedes them
        cat = category(name)
        cat_tot[cat] += 1
        stats["hashed"] += 1

        hit = None
        variants = {}
        if fmt == 2:  # CI4 / CI8 -> needs a TLUT CRC
            cimax = cimax_ci4(data, w, h, row_stride) if siz == 0 else cimax_ci8(data, w, h, row_stride)
            cands = []
            for b in texture_bases(name):
                for pn in pals.get(b, []):
                    if pn not in cands:
                        cands.append(pn)
            for pal_name in cands:
                pcrc = palette_crc(pal_name, siz, cimax)
                if pcrc is not None and (crc, pcrc) in pack:
                    # This LUS fork draws a CI tile with "alt/<raster>@<palette
                    # basename>" when the pack has art for the palette actually
                    # bound (interpreter.cpp ResolvePaletteVariant), so EVERY
                    # matching palette is worth emitting, not just the first.
                    variants.setdefault(pal_name.rsplit("/", 1)[-1],
                                        pack[(crc, pcrc)]["name"])
                    if hit is None:
                        hit = (crc, pcrc, pal_name)
            if hit is None and crc in pack_by_tex:
                # named association missed: search every palette in the archive
                gidx = global_palettes(siz, cimax)
                for pcrc in pack_by_tex[crc]:
                    if pcrc in gidx:
                        hit = (crc, pcrc, gidx[pcrc][0])
                        stats["hit-via-global-palette"] += 1
                        break
            if hit is None and (crc, 0) in pack:
                hit = (crc, 0, None)
                stats["ci-hit-without-palette"] += 1
        else:
            if (crc, 0) in pack:
                hit = (crc, 0, None)

        if hit:
            matched[name] = {
                "png": pack[(hit[0], hit[1])]["name"],
                "palette": hit[2],
                "tex_crc": f"{hit[0]:08X}",
                "pal_crc": f"{hit[1]:08X}",
                "w": w, "h": h, "type": ttype, "cat": cat,
                "pack_w": pack[(hit[0], hit[1])]["width"],
                "pack_h": pack[(hit[0], hit[1])]["height"],
                "variants": variants,
            }
            stats["palette-variants"] += max(0, len(variants) - 1)
            cat_hit[cat] += 1
            stats["hit"] += 1
        else:
            stats["miss"] += 1
            if crc in pack_by_tex:
                texcrc_only[name] = f"{crc:08X}"

    print(f"\nhits {stats['hit']}  misses {stats['miss']}  "
          f"(texCRC matched but palette CRC did not: {len(texcrc_only)})", file=sys.stderr)
    print(f"archive coverage: {stats['hit']}/{stats['hashed']} "
          f"({100.0*stats['hit']/max(stats['hashed'],1):.1f}%)", file=sys.stderr)
    print(f"pack coverage:    {len(set((m['tex_crc'],m['pal_crc']) for m in matched.values()))}/{len(pack)}",
          file=sys.stderr)
    print("\nby category:", file=sys.stderr)
    for c in sorted(cat_tot):
        print(f"  {c:8s} {cat_hit[c]:6d}/{cat_tot[c]:6d}  "
              f"{100.0*cat_hit[c]/max(cat_tot[c],1):5.1f}%", file=sys.stderr)
    print(f"\nother stats: {dict(stats)}", file=sys.stderr)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({
            "archive": str(args.archive),
            "pack": str(args.pack),
            "pack_entries": len(pack),
            "archive_textures": stats["hashed"],
            "hits": stats["hit"],
            "misses": stats["miss"],
            "texcrc_only": texcrc_only,
            "by_category": {c: [cat_hit[c], cat_tot[c]] for c in sorted(cat_tot)},
            "matches": matched,
        }, indent=1))
        print(f"report -> {args.report}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
