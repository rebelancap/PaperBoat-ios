#!/usr/bin/env python3
"""
build-pack-o2r.py — write a PaperBoat alt-asset .o2r.

Two modes:

  --magenta v0|v1   positive control: replace a handful of named assets with
                    flat magenta, so a screenshot proves the substitution path
                    is reached at all (program spec: prove it before
                    converting 2 GB of pack).
  --matches <json>  production: consume scripts/pack-crc-match.py's report and
                    emit the whole matched HD set.

Record format (docs/asset-pipeline.md):

  V0 — `PM64::ResourceFactoryBinaryTextureV0` reads {type,w,h,size} and the
       raw texels. It has NO Flags/HByteScale/VPixelScale, so the texels must
       be in the ORIGINAL N64 format at the ORIGINAL dimensions: a V0 record
       cannot carry an upscale. This is what every record Torch writes is.
  V1 — `Fast::ResourceFactoryBinaryTextureV1` adds {Flags,HByteScale,
       VPixelScale}. With TEX_FLAG_LOAD_AS_RAW the interpreter takes
       `ImportTextureRaw` and treats the payload as RGBA8888, accepting it
       directly when
           origLineSizeBytes * HByteScale == 4 * newWidth
           origHeight        * VPixelScale == newHeight
       (libultraship src/fast/interpreter.cpp:1832-1838). HByteScale is a
       *byte* scale, not a width scale — replacing a CI4 texture at 2x means
       HByteScale 16, not 2.

Both are written under the `alt/` prefix, which LUS's ResourceManager prefers
when CVar gEnhancements.Mods.AlternateAssets is set.
"""

import argparse
import json
import struct
import sys
import zipfile
from pathlib import Path

OTR_HEADER_SIZE = 64
RESTYPE_TEXTURE = 0x4F544558
DEFAULT_ID = 0xDEADBEEFDEADBEEF
TEX_FLAG_LOAD_AS_RAW = 1 << 0
TEXTYPE_RGBA32 = 1
ALT_PREFIX = "alt/"

# Fast::TextureType -> bytes per texel (as the N64 stores them)
BYTES_PER_TEXEL = {1: 4.0, 2: 2.0, 3: 0.5, 4: 1.0, 5: 0.5, 6: 1.0, 7: 0.5, 8: 1.0, 9: 2.0}

# Flat magenta in each N64 texel format the control touches.
#   RGBA16 is RGBA5551 stored big-endian: R=31 G=0 B=31 A=1 -> 0xF83F
MAGENTA_TEXEL = {
    1: b"\xff\x00\xff\xff",   # RGBA32
    2: b"\xf8\x3f",           # RGBA16
}


def _header(version: int) -> bytearray:
    hdr = bytearray(OTR_HEADER_SIZE)
    struct.pack_into("<BBxx", hdr, 0, 0, 1)      # little-endian, isCustom
    struct.pack_into("<I", hdr, 4, RESTYPE_TEXTURE)
    struct.pack_into("<I", hdr, 8, version)
    struct.pack_into("<Q", hdr, 12, DEFAULT_ID)
    return hdr


def encode_texture_v0(ttype: int, w: int, h: int, texels: bytes) -> bytes:
    return bytes(_header(0)) + struct.pack("<IIII", ttype, w, h, len(texels)) + texels


def encode_texture_v1_raw(rgba: bytes, w: int, h: int, hbs: float, vps: float) -> bytes:
    body = struct.pack("<IIIIffI", TEXTYPE_RGBA32, w, h, TEX_FLAG_LOAD_AS_RAW, hbs, vps, len(rgba))
    return bytes(_header(1)) + body + rgba


def scales(orig_w: int, orig_h: int, ttype: int, new_w: int, new_h: int):
    """HByteScale / VPixelScale for an RGBA8888 replacement (interpreter.cpp:1832)."""
    orig_line_bytes = orig_w * BYTES_PER_TEXEL[ttype]
    return (4.0 * new_w) / orig_line_bytes, float(new_h) / float(orig_h)


def read_texture(raw: bytes):
    if len(raw) < OTR_HEADER_SIZE + 16 or struct.unpack_from("<I", raw, 4)[0] != RESTYPE_TEXTURE:
        return None
    ver = struct.unpack_from("<I", raw, 8)[0]
    o = OTR_HEADER_SIZE
    if ver == 0:
        ttype, w, h, sz = struct.unpack_from("<IIII", raw, o)
        return ttype, w, h, raw[o + 16: o + 16 + sz]
    if ver == 1:
        ttype, w, h, fl, hs, vs, sz = struct.unpack_from("<IIIIffI", raw, o)
        return ttype, w, h, raw[o + 28: o + 28 + sz]
    return None


MODS_TOML = '[mod]\nname = "{name}"\nversion = "{version}"\n'


def build_magenta(args):
    z = zipfile.ZipFile(args.archive)
    targets = [t for t in args.targets.split(",") if t]
    out = {}
    for path in targets:
        try:
            raw = z.read(path)
        except KeyError:
            print(f"FATAL: {path} not in {args.archive}", file=sys.stderr)
            return 2
        r = read_texture(raw)
        if r is None:
            print(f"FATAL: {path} is not a Texture record", file=sys.stderr)
            return 2
        ttype, w, h, _texels = r
        if args.magenta == "v0":
            if ttype not in MAGENTA_TEXEL:
                print(f"skip {path}: type {ttype} has no magenta texel (V0 must keep the N64 format)",
                      file=sys.stderr)
                continue
            texel = MAGENTA_TEXEL[ttype]
            out[ALT_PREFIX + path] = encode_texture_v0(ttype, w, h, texel * (w * h))
            print(f"v0  {path}: type {ttype} {w}x{h} same-dims magenta", file=sys.stderr)
        else:
            nw, nh = w * args.scale, h * args.scale
            hbs, vps = scales(w, h, ttype, nw, nh)
            out[ALT_PREFIX + path] = encode_texture_v1_raw(
                b"\xff\x00\xff\xff" * (nw * nh), nw, nh, hbs, vps)
            print(f"v1  {path}: type {ttype} {w}x{h} -> {nw}x{nh} "
                  f"HByteScale={hbs} VPixelScale={vps}", file=sys.stderr)
    if not out:
        print("FATAL: nothing to write", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.out, "w", compression=zipfile.ZIP_STORED) as zo:
        zo.writestr("mods.toml", MODS_TOML.format(name=args.mod_name, version=args.mod_version))
        for k, v in sorted(out.items()):
            zo.writestr(k, v)
    print(f"wrote {args.out}: {len(out)} records, {args.out.stat().st_size} bytes", file=sys.stderr)
    return 0


def build_pack(args):
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    rep = json.loads(args.matches.read_text())
    matches = rep["matches"]
    pack_dir = args.pack
    args.out.parent.mkdir(parents=True, exist_ok=True)
    written = variants = skipped = 0

    def load(png_name, van_w=None, van_h=None):
        """Decode a pack PNG, snapping it to an INTEGER multiple of the vanilla
        size. MasterKillua's pack is an AI upscale with arbitrary dimensions
        (4.25x, 4.62x, …), but ImportTextureRaw only accepts a replacement whose
        scaled line size lands exactly on 4*width — and TileRasterRegion rounds
        HByteScale/VPixelScale to integers — so a fractional pack image has to
        be resampled onto the nearest integer factor or it cannot be used at
        all. Without this only 1,598 of 4,983 matches are usable."""
        png = pack_dir / png_name
        if not png.exists():
            return None
        with Image.open(png) as im:
            arr = im.convert("RGBA")
            if van_w and van_h and not args.no_snap:
                k = max(1, round(((arr.size[0] / van_w) + (arr.size[1] / van_h)) / 2))
                target = (van_w * k, van_h * k)
                if arr.size != target:
                    arr = arr.resize(target, Image.LANCZOS)
            return arr.size, arr.tobytes()

    # pm64.o2r itself is a DEFLATE zip, so the archive reader handles deflated
    # entries; 1.9 GB of RGBA is not something to ship STORED.
    with zipfile.ZipFile(args.out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zo:
        zo.writestr("mods.toml", MODS_TOML.format(name=args.mod_name, version=args.mod_version))
        for asset, m in sorted(matches.items()):
            got = load(m["png"], m["w"], m["h"])
            if got is None:
                skipped += 1
                continue
            (nw, nh), rgba = got
            hbs, vps = scales(m["w"], m["h"], m["type"], nw, nh)
            if abs(hbs - round(hbs)) > 1e-6 or abs(vps - round(vps)) > 1e-6:
                # Non-integer scale: the interpreter's HD paths round these, so a
                # fractional upscale would sample wrong. Report, never guess.
                skipped += 1
                continue
            zo.writestr(ALT_PREFIX + asset, encode_texture_v1_raw(rgba, nw, nh, hbs, vps))
            written += 1
            # Per-palette variants: "alt/<raster>@<palette basename>". The
            # interpreter rejects a variant whose dimensions or type differ from
            # the base replacement, so only same-size ones are emitted.
            for pal_base, png_name in sorted(m.get("variants", {}).items()):
                if png_name == m["png"]:
                    continue
                got = load(png_name, m["w"], m["h"])
                if got is None or got[0] != (nw, nh):
                    skipped += 1
                    continue
                zo.writestr(ALT_PREFIX + asset + "@" + pal_base,
                            encode_texture_v1_raw(got[1], nw, nh, hbs, vps))
                variants += 1
            if written % 500 == 0:
                print(f"  {written} …", file=sys.stderr)
    print(f"wrote {args.out}: {written} textures + {variants} palette variants, "
          f"{skipped} skipped, {args.out.stat().st_size / 1048576:.1f} MB", file=sys.stderr)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", type=Path, default=Path("oracle/shiphome/pm64.o2r"))
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--mod-name", default="PaperBoat-HD")
    ap.add_argument("--mod-version", default="0.1.0")
    ap.add_argument("--magenta", choices=["v0", "v1"])
    ap.add_argument("--targets", default="")
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument("--matches", type=Path)
    ap.add_argument("--pack", type=Path)
    ap.add_argument("--no-snap", action="store_true",
                    help="do not resample fractional upscales onto an integer factor")
    args = ap.parse_args()
    if args.magenta:
        return build_magenta(args)
    if args.matches and args.pack:
        return build_pack(args)
    ap.error("need --magenta v0|v1 --targets …, or --matches <json> --pack <dir>")


if __name__ == "__main__":
    sys.exit(main())
