#!/usr/bin/env python3
"""
hts-decode.py — decode a GLideN64 `.hts` / `.htc` hi-res texture cache into
CRC-named PNGs plus a JSON index.

Format derived from GLideN64 source (gonetz/GLideN64, src/GLideNHQ/TxCache.cpp,
class TxFileStorage: open/save/load/writeData/readData) — NOT from memory.
See docs/texture-pack-hts.md for the offset table.

Layout (little-endian throughout; `.hts` is a plain file, NOT gzipped —
that is the older `.htc` TxMemoryCache path, which wraps the whole stream
in gzip):

    CURRENT version (int32 at 0 == TXCACHE_FORMAT_VERSION 0x08000000):
        0x00 int32  version   (0x08000000)
        0x04 int32  config    (TxFilter config bits, or -1 == "unsaved")
        0x08 int64  storagePos    (byte offset of the checksum->offset map)
        0x10 ...    texture entries, packed back to back
    OLD version (int32 at 0 != 0x08000000):
        0x00 int32  config
        0x04 int64  storagePos
        0x0C ...    texture entries

    entry @ offset:
        int32  width
        int32  height
        uint32 format          GL internal format; bit 31 (GL_TEXFMT_GZ
                               0x80000000) means the payload is zlib-deflated
        uint16 texture_format  GL format enum   (e.g. GL_RGBA 0x1908)
        uint16 pixel_type      GL type enum     (e.g. GL_UNSIGNED_BYTE 0x1401)
        uint8  is_hires_tex
        uint16 n64_format_size  <-- CURRENT VERSION ONLY (absent in OLD)
        uint32 dataSize        bytes on disk (compressed size if GZ)
        uint8  data[dataSize]

    map @ storagePos:
        int32 count
        count * { uint64 checksum ; int64 packed }
            packed._offset    = bits 0..47   (file offset of the entry)
            packed._formatsize= bits 48..63  (0 in OLD-version files)
        checksum = (palette_crc32 << 32) | texture_crc32   (TxUtil::checksum64)
        ...except when the source filename used the `#$#` wildcard form, where
        the loader stores the palette crc ALONE in the low 32 bits
        (TxHiResCache.cpp:240-244). Indistinguishable from palette_crc32 == 0.

Filename contract (Lighthouse-ios/scripts/pack-crc-match.py PACK_RE):
    <rom>#<texCRC 8hex>#<fmt 1hex>#<siz 1hex>[#<palCRC 8hex>]_<kind>.png
OLD-version caches do not store fmt/siz (see the gap note in the docs), so
those entries are written as
    <rom>#<texCRC 8hex>#<palCRC 8hex|none>_all.png
and the full key detail lives in index.json next to them.
"""

import argparse
import collections
import json
import os
import struct
import sys
import zlib
from pathlib import Path

TXCACHE_FORMAT_VERSION = 0x08000000      # TxFilterExport.h: UNDEFINED_1
GL_TEXFMT_GZ = 0x80000000                # TxInternal.h

GL_NAMES = {
    0x1907: "GL_RGB", 0x1908: "GL_RGBA",
    0x8056: "GL_RGBA4", 0x8057: "GL_RGB5_A1", 0x8058: "GL_RGBA8",
    0x8051: "GL_RGB8", 0x8D62: "GL_RGB565",
    0x83F0: "GL_COMPRESSED_RGB_S3TC_DXT1",
    0x83F1: "GL_COMPRESSED_RGBA_S3TC_DXT1",
    0x83F2: "GL_COMPRESSED_RGBA_S3TC_DXT3",
    0x83F3: "GL_COMPRESSED_RGBA_S3TC_DXT5",
    0x8B90: "GL_PALETTE4_RGB8_OES",
}
TYPE_NAMES = {
    0x1401: "GL_UNSIGNED_BYTE",
    0x8033: "GL_UNSIGNED_SHORT_4_4_4_4",
    0x8034: "GL_UNSIGNED_SHORT_5_5_5_1",
    0x8363: "GL_UNSIGNED_SHORT_5_6_5",
    0x8365: "GL_UNSIGNED_SHORT_4_4_4_4_REV",
    0x8366: "GL_UNSIGNED_SHORT_1_5_5_5_REV",
}


def gl_name(v):
    return GL_NAMES.get(v, "0x%04X" % v)


def type_name(v):
    return TYPE_NAMES.get(v, "0x%04X" % v)


# ---------------------------------------------------------------- PNG writer
def _chunk(tag, payload):
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))


def write_png_rgba(path, width, height, rgba):
    """rgba: bytes, width*height*4, row-major, top-down."""
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)                                # filter type 0 (None)
        raw += rgba[y * stride:(y + 1) * stride]
    png = (b"\x89PNG\r\n\x1a\n"
           + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
           + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
           + _chunk(b"IEND", b""))
    path.write_bytes(png)


# ------------------------------------------------------------ pixel decoding
def to_rgba(width, height, fmt, pixel_type, data):
    """Return width*height*4 RGBA bytes, or None if the format is unhandled."""
    base = fmt & ~GL_TEXFMT_GZ
    n = width * height
    if base in (0x8058, 0x1908) and pixel_type == 0x1401:      # RGBA8888
        if len(data) < n * 4:
            return None
        return data[:n * 4]
    if base == 0x8051 and pixel_type == 0x1401:                # RGB888
        if len(data) < n * 3:
            return None
        out = bytearray(n * 4)
        for i in range(n):
            out[i * 4:i * 4 + 3] = data[i * 3:i * 3 + 3]
            out[i * 4 + 3] = 0xFF
        return bytes(out)
    if len(data) >= n * 2:
        out = bytearray(n * 4)
        if pixel_type == 0x8033:                               # RGBA4444
            for i in range(n):
                v = data[i * 2] | (data[i * 2 + 1] << 8)
                r, g, b, a = (v >> 12) & 15, (v >> 8) & 15, (v >> 4) & 15, v & 15
                out[i * 4:i * 4 + 4] = bytes((r * 17, g * 17, b * 17, a * 17))
            return bytes(out)
        if pixel_type == 0x8034:                               # RGB5_A1
            for i in range(n):
                v = data[i * 2] | (data[i * 2 + 1] << 8)
                r, g, b, a = (v >> 11) & 31, (v >> 6) & 31, (v >> 1) & 31, v & 1
                out[i * 4:i * 4 + 4] = bytes((r * 255 // 31, g * 255 // 31,
                                              b * 255 // 31, 255 if a else 0))
            return bytes(out)
        if pixel_type == 0x8363:                               # RGB565
            for i in range(n):
                v = data[i * 2] | (data[i * 2 + 1] << 8)
                r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
                out[i * 4:i * 4 + 4] = bytes((r * 255 // 31, g * 255 // 63,
                                              b * 255 // 31, 255))
            return bytes(out)
    return None


# ------------------------------------------------------------------- reading
class HtsFile:
    def __init__(self, path):
        self.path = Path(path)
        self.f = open(path, "rb")
        self.file_size = self.path.stat().st_size
        head = self.f.read(16)
        if len(head) < 16:
            raise SystemExit("%s: too short to be an .hts" % path)
        if head[:2] == b"\x1f\x8b":
            raise SystemExit("%s: gzip stream — this is a TxMemoryCache .htc, "
                             "not a TxFileStorage .hts" % path)
        version = struct.unpack_from("<I", head, 0)[0]
        if version == TXCACHE_FORMAT_VERSION:
            self.old = False
            self.version = version
            self.config = struct.unpack_from("<i", head, 4)[0]
            self.storage_pos = struct.unpack_from("<q", head, 8)[0]
            self.initial_pos = 16
        else:
            self.old = True
            self.version = None
            self.config = version
            self.storage_pos = struct.unpack_from("<q", head, 4)[0]
            self.initial_pos = 12
        if not (self.initial_pos < self.storage_pos <= self.file_size):
            raise SystemExit("%s: storagePos %d out of range (file %d)"
                             % (path, self.storage_pos, self.file_size))

    def index(self):
        """[(checksum, offset, formatsize)] — read from the tail map."""
        self.f.seek(self.storage_pos)
        (count,) = struct.unpack("<i", self.f.read(4))
        if count <= 0:
            raise SystemExit("%s: bad storage count %d" % (self.path, count))
        blob = self.f.read(count * 16)
        if len(blob) != count * 16:
            raise SystemExit("%s: truncated storage map" % self.path)
        out = []
        for i in range(count):
            chk, packed = struct.unpack_from("<Qq", blob, i * 16)
            offset = packed & ((1 << 48) - 1)
            fs = (packed >> 48) & 0xFFFF
            out.append((chk, offset, fs))
        return out

    def read_entry(self, offset):
        self.f.seek(offset)
        hdr = self.f.read(21 if self.old else 23)
        width, height, fmt = struct.unpack_from("<iiI", hdr, 0)
        tex_fmt, pix_type = struct.unpack_from("<HH", hdr, 12)
        is_hires = hdr[16]
        if self.old:
            fmtsize = None
            (data_size,) = struct.unpack_from("<I", hdr, 17)
        else:
            (fmtsize,) = struct.unpack_from("<H", hdr, 17)
            (data_size,) = struct.unpack_from("<I", hdr, 19)
        data = self.f.read(data_size)
        return dict(width=width, height=height, format=fmt, texture_format=tex_fmt,
                    pixel_type=pix_type, is_hires_tex=is_hires, formatsize=fmtsize,
                    data_size=data_size, data=data)


def split_checksum(chk):
    """-> (texCRC, palCRC or None).  TxUtil::checksum64 packs hi=palette."""
    lo = chk & 0xFFFFFFFF
    hi = (chk >> 32) & 0xFFFFFFFF
    return lo, (hi if hi else None)


def entry_name(rom, tex_crc, pal_crc, fmtsize, kind="all"):
    if fmtsize:
        fmt = (fmtsize >> 8) & 0xFF
        siz = fmtsize & 0xFF
        pal = "#%08X" % pal_crc if pal_crc is not None else ""
        return "%s#%08X#%X#%X%s_%s.png" % (rom, tex_crc, fmt, siz, pal, kind)
    pal = "%08X" % pal_crc if pal_crc is not None else "none"
    return "%s#%08X#%s_%s.png" % (rom, tex_crc, pal, kind)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("hts", type=Path)
    ap.add_argument("--out", type=Path, help="write PNGs + index.json here")
    ap.add_argument("--index-only", action="store_true",
                    help="scan headers, write no PNGs (use for the 2 GB pack)")
    ap.add_argument("--index-json", type=Path, help="write the index JSON here")
    ap.add_argument("--rom", help="ROM ident for filenames "
                                  "(default: from the .hts filename)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N entries")
    args = ap.parse_args()

    rom = args.rom or args.hts.name.split("_HIRESTEXTURES")[0]
    h = HtsFile(args.hts)
    idx = h.index()
    idx.sort(key=lambda t: t[1])          # sequential IO over a 2 GB file

    print("file        : %s (%.1f MB)" % (args.hts, h.file_size / 1048576.0))
    print("layout      : %s-version TxFileStorage" % ("OLD" if h.old else "CURRENT"))
    print("config      : 0x%08X   storagePos: %d (0x%X)"
          % (h.config & 0xFFFFFFFF, h.storage_pos, h.storage_pos))
    print("rom ident   : %s" % rom)
    print("entries     : %d" % len(idx))

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)

    dims = collections.Counter()
    fmts = collections.Counter()
    types = collections.Counter()
    stats = collections.Counter()
    total_raw = 0
    total_disk = 0
    records = []

    for i, (chk, off, fs) in enumerate(idx):
        if args.limit and i >= args.limit:
            break
        try:
            e = h.read_entry(off)
        except Exception as ex:
            stats["header-error"] += 1
            continue
        tex_crc, pal_crc = split_checksum(chk)
        gz = bool(e["format"] & GL_TEXFMT_GZ)
        stats["compressed" if gz else "raw"] += 1
        dims[(e["width"], e["height"])] += 1
        fmts[gl_name(e["format"] & ~GL_TEXFMT_GZ)] += 1
        types[type_name(e["pixel_type"])] += 1
        total_disk += e["data_size"]

        name = entry_name(rom, tex_crc, pal_crc, e["formatsize"] if fs == 0 else fs)
        rec = dict(name=name, tex_crc="%08X" % tex_crc,
                   pal_crc=("%08X" % pal_crc) if pal_crc is not None else None,
                   checksum64="%016X" % chk, offset=off,
                   width=e["width"], height=e["height"],
                   gl_format=gl_name(e["format"] & ~GL_TEXFMT_GZ),
                   pixel_type=type_name(e["pixel_type"]),
                   texture_format=gl_name(e["texture_format"]),
                   is_hires_tex=e["is_hires_tex"], compressed=gz,
                   disk_bytes=e["data_size"])

        if not args.index_only:
            data = e["data"]
            if gz:
                try:
                    data = zlib.decompress(data)
                except zlib.error:
                    stats["inflate-error"] += 1
                    records.append(rec)
                    continue
            total_raw += len(data)
            rec["raw_bytes"] = len(data)
            rgba = to_rgba(e["width"], e["height"], e["format"], e["pixel_type"], data)
            if rgba is None:
                stats["unhandled-format"] += 1
            elif args.out:
                write_png_rgba(args.out / name, e["width"], e["height"], rgba)
                stats["png"] += 1
        records.append(rec)
        if (i + 1) % 500 == 0:
            print("  ... %d/%d" % (i + 1, len(idx)), file=sys.stderr)

    print("\n-- summary ------------------------------------------------")
    print("entries scanned : %d" % len(records))
    print("compressed      : %d" % stats["compressed"])
    print("raw             : %d" % stats["raw"])
    print("PNGs written    : %d" % stats["png"])
    for k in ("unhandled-format", "inflate-error", "header-error"):
        if stats[k]:
            print("%-16s: %d" % (k, stats[k]))
    print("bytes on disk   : %d (%.1f MB)" % (total_disk, total_disk / 1048576.0))
    if total_raw:
        print("bytes decoded   : %d (%.1f MB)" % (total_raw, total_raw / 1048576.0))
    print("\nGL internal format histogram:")
    for k, v in fmts.most_common():
        print("  %-32s %6d" % (k, v))
    print("pixel type histogram:")
    for k, v in types.most_common():
        print("  %-32s %6d" % (k, v))
    print("dimension histogram (top 25 of %d distinct):" % len(dims))
    for (w, hh), v in dims.most_common(25):
        print("  %5d x %-5d %6d" % (w, hh, v))
    px = sum(w * hh * c for (w, hh), c in dims.items())
    print("total pixels    : %d (%.1f Mpx)" % (px, px / 1e6))

    out_json = args.index_json or (args.out / "index.json" if args.out else None)
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(dict(
            source=str(args.hts), rom=rom, old_version=h.old,
            config="0x%08X" % (h.config & 0xFFFFFFFF),
            storage_pos=h.storage_pos, entry_count=len(idx),
            fmt_siz_available=not h.old,
            entries=records), indent=1))
        print("\nindex -> %s" % out_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
