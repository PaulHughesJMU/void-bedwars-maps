"""Turn World Downloader saves of void maps into real void worlds.

A downloaded map is the build plus a ring of empty chunks (the downloader's view
distance), saved with a "default" level.dat, so a server generates normal terrain
right past the ring. This keeps only the chunks that hold the map, writes a
level.dat whose generator is void (flat, single air layer), and drops downloader
leftovers (playerdata, stats, End/Nether folders, dropped items, uid.dat).

    python tools/voidify.py <world dir | world zip> <out dir> [--arena arena.yml]
    python tools/voidify.py --tree <minigames-Source checkout> <out root>

Needs numpy; Pillow for previews and PyYAML for arena files are optional.
"""
import argparse
import json
import os
import re
import shutil
import struct
import sys
import zipfile
import zlib
import gzip
from concurrent.futures import ProcessPoolExecutor

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nbt
from nbt import Tag, TAG_BYTE, TAG_SHORT, TAG_INT, TAG_LONG, TAG_DOUBLE, TAG_BYTE_ARRAY, TAG_STRING, TAG_LIST, TAG_COMPOUND

try:
    import yaml
except ImportError:
    yaml = None
try:
    import preview
except ImportError:
    preview = None

VOID_PRESET = "2;0;1;"
FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)
KEEP_ENTITIES = {"ItemFrame", "Painting", "ArmorStand"}
ENTITY_ID_18 = {"minecraft:item_frame": "ItemFrame", "minecraft:painting": "Painting",
                "minecraft:armor_stand": "ArmorStand"}
# 1.11+ servers rewrote these; a 1.8 server skips tile entities it doesn't know
TILE_ID_18 = {
    "minecraft:furnace": "Furnace", "minecraft:chest": "Chest", "minecraft:ender_chest": "EnderChest",
    "minecraft:jukebox": "RecordPlayer", "minecraft:dispenser": "Trap", "minecraft:dropper": "Dropper",
    "minecraft:sign": "Sign", "minecraft:mob_spawner": "MobSpawner", "minecraft:noteblock": "Music",
    "minecraft:piston": "Piston", "minecraft:brewing_stand": "Cauldron", "minecraft:enchanting_table": "EnchantTable",
    "minecraft:end_portal": "Airportal", "minecraft:beacon": "Beacon", "minecraft:skull": "Skull",
    "minecraft:daylight_detector": "DLDetector", "minecraft:hopper": "Hopper", "minecraft:comparator": "Comparator",
    "minecraft:flower_pot": "FlowerPot", "minecraft:banner": "Banner", "minecraft:command_block": "Control",
}
REGION_RE = re.compile(r"^(?:[^/]+/)?region/r\.(-?\d+)\.(-?\d+)\.mca$")
LEVEL_RE = re.compile(r"^(?:[^/]+/)?level\.dat$")


# ---------------------------------------------------------------- reading

def read_source(src):
    """-> ({(rx, rz): bytes}, level.dat bytes or None). Overworld only."""
    regions, level = {}, None
    if os.path.isfile(src):
        with zipfile.ZipFile(src) as z:
            for n in z.namelist():
                n2 = n.replace("\\", "/")
                if n2.split("/")[0] in ("DIM1", "DIM-1"):
                    continue
                m = REGION_RE.match(n2)
                if m:
                    regions[(int(m.group(1)), int(m.group(2)))] = z.read(n)
                elif LEVEL_RE.match(n2) and level is None:
                    level = z.read(n)
    else:
        rd = os.path.join(src, "region")
        for n in os.listdir(rd) if os.path.isdir(rd) else []:
            m = REGION_RE.match("region/" + n)
            if m:
                with open(os.path.join(rd, n), "rb") as f:
                    regions[(int(m.group(1)), int(m.group(2)))] = f.read()
        lp = os.path.join(src, "level.dat")
        if os.path.isfile(lp):
            with open(lp, "rb") as f:
                level = f.read()
    return regions, level


class Chunk:
    __slots__ = ("x", "z", "ts", "name", "root", "blocks", "terrain")


def load_chunks(regions):
    chunks = {}
    for (rx, rz), data in regions.items():
        if len(data) < 8192:
            continue
        for i in range(1024):
            loc = struct.unpack_from(">I", data, i * 4)[0]
            off, cnt = loc >> 8, loc & 0xFF
            if not off or not cnt or off * 4096 + 5 > len(data):
                continue
            p = off * 4096
            ln, comp = struct.unpack_from(">IB", data, p)
            raw = data[p + 5:p + 4 + ln]
            try:
                body = zlib.decompress(raw) if comp == 2 else gzip.decompress(raw) if comp == 1 else raw
                name, root = nbt.loads(body)
            except Exception:
                continue
            c = Chunk()
            c.x, c.z = rx * 32 + i % 32, rz * 32 + i // 32
            c.ts = struct.unpack_from(">I", data, 4096 + i * 4)[0]
            c.name, c.root = name, root
            c.blocks, c.terrain = 0, False
            for s in sections(root):
                b = np.frombuffer(s["Blocks"].value, np.uint8)
                c.blocks += int(np.count_nonzero(b))
                # vanilla terrain always has a bedrock floor at y=0; builds almost never do
                if s["Y"].value == 0 and np.count_nonzero(b[:256] == 7) > 200:
                    c.terrain = True
            chunks[(c.x, c.z)] = c
    return chunks


def sections(root):
    s = root["Level"].get("Sections")
    return s.value if s is not None else []


# ---------------------------------------------------------------- selection

def select(chunks):
    """Keep every connected group of non-empty chunks, except groups that run into
    unsaved space and contain generated terrain (bedrock floor)."""
    saved = set(chunks)
    nonvoid = {k for k, c in chunks.items() if c.blocks}
    keep, dropped, warnings = set(), set(), []
    seen = set()
    for start in sorted(nonvoid):
        if start in seen:
            continue
        comp, stack = {start}, [start]
        seen.add(start)
        while stack:
            x, z = stack.pop()
            for n in ((x + 1, z), (x - 1, z), (x, z + 1), (x, z - 1)):
                if n in nonvoid and n not in seen:
                    seen.add(n)
                    comp.add(n)
                    stack.append(n)
        edge = any(n not in saved for x, z in comp for n in ((x + 1, z), (x - 1, z), (x, z + 1), (x, z - 1)))
        terrain = any(chunks[k].terrain for k in comp)
        if edge and terrain:
            dropped |= comp
        else:
            keep |= comp
            if edge:
                warnings.append("chunks at %s touch the edge of the download (no empty ring)" % (min(comp),))
            if terrain:
                warnings.append("kept %d chunk(s) with a bedrock floor near %s" % (
                    sum(chunks[k].terrain for k in comp), min(comp)))
    return keep, dropped, warnings


# ---------------------------------------------------------------- cleaning

def clean(chunk):
    lvl = chunk.root["Level"]
    dropped = fixed = 0
    ents = lvl.get("Entities")
    if ents is not None:
        kept = []
        for e in ents.value:
            eid = e.get("id")
            eid = ENTITY_ID_18.get(eid.value, eid.value) if eid is not None else None
            if eid in KEEP_ENTITIES:
                e.value["id"] = Tag(TAG_STRING, eid)
                kept.append(e)
            else:
                dropped += 1
        ents.value = kept
        if not kept:
            ents.elem = TAG_COMPOUND
    tiles = lvl.get("TileEntities")
    for t in tiles.value if tiles is not None else []:
        tid = t.get("id")
        if tid is not None and tid.value in TILE_ID_18:
            tid.value = TILE_ID_18[tid.value]
            fixed += 1
    return dropped, fixed


# ---------------------------------------------------------------- writing

def write_regions(region_dir, chunks):
    os.makedirs(region_dir, exist_ok=True)
    by_region = {}
    for c in chunks:
        by_region.setdefault((c.x >> 5, c.z >> 5), []).append(c)
    for (rx, rz), cs in sorted(by_region.items()):
        header = bytearray(8192)
        body = bytearray()
        sector = 2
        for c in sorted(cs, key=lambda c: ((c.z & 31) * 32 + (c.x & 31))):
            data = zlib.compress(nbt.dumps(c.name, c.root), 9)
            payload = struct.pack(">IB", len(data) + 1, 2) + data
            n = (len(payload) + 4095) // 4096
            payload += b"\0" * (n * 4096 - len(payload))
            idx = (c.z & 31) * 32 + (c.x & 31)
            struct.pack_into(">I", header, idx * 4, (sector << 8) | n)
            struct.pack_into(">I", header, 4096 + idx * 4, c.ts)
            body += payload
            sector += n
        with open(os.path.join(region_dir, "r.%d.%d.mca" % (rx, rz)), "wb") as f:
            f.write(header + body)


def level_dat(name, spawn, old):
    seed = 0
    if old:
        try:
            seed = nbt.read_gz(old)[1]["Data"]["RandomSeed"].value
        except Exception:
            pass
    b = lambda v: Tag(TAG_BYTE, v)
    i = lambda v: Tag(TAG_INT, v)
    l = lambda v: Tag(TAG_LONG, v)
    d = lambda v: Tag(TAG_DOUBLE, v)
    s = lambda v: Tag(TAG_STRING, v)
    rules = {"doDaylightCycle": "false", "doMobSpawning": "false", "doFireTick": "false",
             "randomTickSpeed": "0", "doTileDrops": "true", "doMobLoot": "true", "doEntityDrops": "true",
             "keepInventory": "false", "mobGriefing": "true", "naturalRegeneration": "true",
             "commandBlockOutput": "true", "logAdminCommands": "true", "sendCommandFeedback": "true",
             "showDeathMessages": "true", "reducedDebugInfo": "false"}
    data = {
        "version": i(19133), "initialized": b(1), "LevelName": s(name),
        "generatorName": s("flat"), "generatorVersion": i(0), "generatorOptions": s(VOID_PRESET),
        "RandomSeed": l(seed), "MapFeatures": b(0), "LastPlayed": l(0), "SizeOnDisk": l(0),
        "allowCommands": b(0), "hardcore": b(0), "GameType": i(0), "Difficulty": b(2), "DifficultyLocked": b(0),
        "Time": l(6000), "DayTime": l(6000),
        "SpawnX": i(spawn[0]), "SpawnY": i(spawn[1]), "SpawnZ": i(spawn[2]),
        "raining": b(0), "rainTime": i(0), "thundering": b(0), "thunderTime": i(0), "clearWeatherTime": i(2 ** 31 - 1),
        "BorderCenterX": d(0.0), "BorderCenterZ": d(0.0), "BorderSize": d(60000000.0), "BorderSafeZone": d(5.0),
        "BorderWarningBlocks": d(5.0), "BorderWarningTime": d(15.0), "BorderSizeLerpTarget": d(60000000.0),
        "BorderSizeLerpTime": l(0), "BorderDamagePerBlock": d(0.2),
        "GameRules": Tag(TAG_COMPOUND, {k: s(v) for k, v in rules.items()}),
    }
    return nbt.dumps("", Tag(TAG_COMPOUND, {"Data": Tag(TAG_COMPOUND, data)}))


def write_world(out, name, chunks, spawn, old_level):
    if os.path.exists(out):
        shutil.rmtree(out)
    write_regions(os.path.join(out, "region"), chunks)
    with open(os.path.join(out, "level.dat"), "wb") as f:
        f.write(gzip.compress(level_dat(name, spawn, old_level), mtime=0))


def zip_world(world_dir, zip_path):
    os.makedirs(os.path.dirname(zip_path), exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in ["level.dat"] + ["region/" + n for n in sorted(os.listdir(os.path.join(world_dir, "region")))]:
            zi = zipfile.ZipInfo(rel, FIXED_ZIP_TIME)
            zi.compress_type = zipfile.ZIP_DEFLATED
            with open(os.path.join(world_dir, rel), "rb") as f:
                z.writestr(zi, f.read())


# ---------------------------------------------------------------- volume, schematic

def build_volume(chunks):
    cx0 = min(c.x for c in chunks); cz0 = min(c.z for c in chunks)
    cx1 = max(c.x for c in chunks); cz1 = max(c.z for c in chunks)
    W, L = (cx1 - cx0 + 1) * 16, (cz1 - cz0 + 1) * 16
    ids = np.zeros((256, L, W), np.uint8)
    dat = np.zeros((256, L, W), np.uint8)
    tiles = []
    for c in chunks:
        ox, oz = (c.x - cx0) * 16, (c.z - cz0) * 16
        for s in sections(c.root):
            y = s["Y"].value * 16
            ids[y:y + 16, oz:oz + 16, ox:ox + 16] = np.frombuffer(s["Blocks"].value, np.uint8).reshape(16, 16, 16)
            nib = np.frombuffer(s["Data"].value, np.uint8)
            d = np.empty(4096, np.uint8)
            d[0::2] = nib & 15
            d[1::2] = nib >> 4
            dat[y:y + 16, oz:oz + 16, ox:ox + 16] = d.reshape(16, 16, 16)
        t = c.root["Level"].get("TileEntities")
        tiles += t.value if t is not None else []
    return ids, dat, (cx0 * 16, 0, cz0 * 16), tiles


def bounds(ids):
    ys = np.flatnonzero(ids.any(axis=(1, 2)))
    zs = np.flatnonzero(ids.any(axis=(0, 2)))
    xs = np.flatnonzero(ids.any(axis=(0, 1)))
    return xs[0], ys[0], zs[0], xs[-1], ys[-1], zs[-1]


def write_schematic(path, ids, dat, origin, tiles, bb):
    """MCEdit .schematic. Clipboard origin is the map's (0, lowest y, 0), so
    //paste -o restores the original coordinates and plain //paste centers on you."""
    x0, y0, z0, x1, y1, z1 = bb
    sub_ids = ids[y0:y1 + 1, z0:z1 + 1, x0:x1 + 1]
    sub_dat = dat[y0:y1 + 1, z0:z1 + 1, x0:x1 + 1]
    H, L, W = sub_ids.shape
    wx, wy, wz = origin[0] + x0, y0, origin[2] + z0
    te = []
    for t in tiles:
        rx, ry, rz = t["x"].value - wx, t["y"].value - wy, t["z"].value - wz
        if 0 <= rx < W and 0 <= ry < H and 0 <= rz < L:
            v = dict(t.value)
            v["x"], v["y"], v["z"] = Tag(TAG_INT, int(rx)), Tag(TAG_INT, int(ry)), Tag(TAG_INT, int(rz))
            te.append(Tag(TAG_COMPOUND, v))
    i = lambda v: Tag(TAG_INT, int(v))
    root = Tag(TAG_COMPOUND, {
        "Width": Tag(TAG_SHORT, W), "Height": Tag(TAG_SHORT, H), "Length": Tag(TAG_SHORT, L),
        "Materials": Tag(TAG_STRING, "Alpha"),
        "Blocks": Tag(TAG_BYTE_ARRAY, sub_ids.tobytes()), "Data": Tag(TAG_BYTE_ARRAY, sub_dat.tobytes()),
        "Entities": Tag(TAG_LIST, [], TAG_COMPOUND), "TileEntities": Tag(TAG_LIST, te, TAG_COMPOUND),
        "WEOriginX": i(wx), "WEOriginY": i(wy), "WEOriginZ": i(wz),
        "WEOffsetX": i(wx), "WEOffsetY": i(0), "WEOffsetZ": i(wz),
    })
    os.makedirs(os.path.dirname(path), exist_ok=True)
    nbt.write_gz(path, "Schematic", root)


# ---------------------------------------------------------------- arena files

def parse_loc(v):
    p = [float(x) for x in str(v).split(",")[:3]]
    return p if len(p) == 3 else None


def read_arena(path):
    if not path or not os.path.isfile(path) or yaml is None:
        return None
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def arena_info(arena):
    if not arena:
        return {}
    w = arena.get("waiting") or {}
    teams = arena.get("Team") or {}
    return {
        "group": arena.get("group"),
        "teams": list(teams),
        "maxInTeam": arena.get("maxInTeam"),
        "waiting": parse_loc(w["Loc"]) if "Loc" in w else None,
        "waitingBox": [parse_loc(w["Pos1"]), parse_loc(w["Pos2"])] if "Pos1" in w and "Pos2" in w else None,
    }


# ---------------------------------------------------------------- driver

def convert(src, name, out_world, arena=None, out_zip=None, out_schematic=None, out_preview=None):
    regions, old_level = read_source(src)
    chunks = load_chunks(regions)
    keep, dropped, warnings = select(chunks)
    info = {"name": name, "source": src.replace("\\", "/"), "warnings": warnings}
    if not keep:
        info["error"] = "no map blocks found"
        return info
    kept = [chunks[k] for k in keep]
    ents = fixed = 0
    for c in kept:
        e, f = clean(c)
        ents += e
        fixed += f
    a = arena_info(arena)
    ids, dat, origin, tiles = build_volume(kept)
    bb = bounds(ids)
    if a.get("waiting"):
        spawn = [int(np.floor(v)) for v in a["waiting"]]
    else:
        cx, cz = -origin[0], -origin[2]
        col = ids[:, cz, cx] if 0 <= cx < ids.shape[2] and 0 <= cz < ids.shape[1] else np.zeros(1)
        top = np.flatnonzero(col)
        spawn = [0, int(top[-1]) + 1 if len(top) else 100, 0]
    write_world(out_world, name, kept, spawn, old_level)
    if out_zip:
        zip_world(out_world, out_zip)
    if out_schematic:
        write_schematic(out_schematic, ids, dat, origin, tiles, bb)
    if out_preview and preview is not None:
        os.makedirs(os.path.dirname(out_preview), exist_ok=True)
        ex = None
        if a.get("waitingBox") and all(a["waitingBox"]):
            p1, p2 = a["waitingBox"]
            lo = [int(np.floor(min(p1[i], p2[i]))) for i in range(3)]
            hi = [int(np.floor(max(p1[i], p2[i]))) for i in range(3)]
            ex = (lo[0] - origin[0], lo[1], lo[2] - origin[2], hi[0] - origin[0], hi[1], hi[2] - origin[2])
        preview.render(out_preview, ids, dat, ex)
    x0, y0, z0, x1, y1, z1 = (int(v) for v in bb)
    info.update({
        "min": [x0 + origin[0], y0, z0 + origin[2]], "max": [x1 + origin[0], y1, z1 + origin[2]],
        "size": [x1 - x0 + 1, y1 - y0 + 1, z1 - z0 + 1],
        "spawn": spawn, "chunks": len(keep), "emptyChunksDropped": len(chunks) - len(keep) - len(dropped),
        "terrainChunksDropped": len(dropped), "entitiesDropped": ents, "tileIdsFixed": fixed,
        "blocks": int(np.count_nonzero(ids)), "hadLevelDat": old_level is not None,
    })
    info.update({k: v for k, v in a.items() if k in ("group", "teams", "maxInTeam")})
    return info


def _has_chunks(src):
    try:
        return bool(load_chunks(read_source(src)[0]))
    except Exception:
        return False


# same map names repeat across modes (archway is 2v2 and 4v4v4v4), so every world gets its mode appended
MODE_TAGS = {"Bedwars2v2(4v4,SoloModded)": "2v2", "Bedwars4v4v4v4(3v3v3v3)": "4v4v4v4",
             "BedwarsRush(SoloModded)": "rush", "BedwarsSolo(doubles)": "solo", "Bedfight(BedwarsRushDuel)": "bedfight",
             "DuelsFlat": "flat", "DuelsShaped": "shaped", "DuelsSumo": "sumo"}


def mode_tag(mode):
    return MODE_TAGS.get(mode) or re.sub(r"\(.*?\)", "", mode).lower()


def tree_jobs(src_root, out_root):
    jobs = []
    hyp = os.path.join(src_root, "Hypixel")
    for mode in sorted(os.listdir(hyp)):
        md, od = os.path.join(hyp, mode), os.path.join(out_root, "Hypixel", mode)
        if not os.path.isdir(md):
            continue
        if os.path.isdir(os.path.join(md, "World")):
            names = set(os.listdir(os.path.join(md, "World")))
            names |= {n[:-4] for n in os.listdir(os.path.join(md, "Cache")) if n.endswith(".zip")}
            for o in sorted(names):
                n = "%s_%s" % (o, mode_tag(mode))
                arena = os.path.join(md, "Arenas", o + ".yml")
                cands = [os.path.join(md, "World", o), os.path.join(md, "Cache", o + ".zip")]
                jobs.append(dict(root=src_root, out=out_root, mode=mode, name=n, orig=o, cands=cands, arena=arena if os.path.isfile(arena) else None,
                                 out_world=os.path.join(od, "World", n),
                                 out_zip=os.path.join(od, "Cache", n + ".zip") if os.path.isfile(arena) else None,
                                 out_arena=os.path.join(od, "Arenas", n + ".yml"),
                                 out_schematic=os.path.join(od, "Schematics", n + ".schematic"),
                                 out_preview=os.path.join(od, "Previews", n + ".png")))
        else:
            for z in sorted(f for f in os.listdir(md) if f.endswith(".zip")):
                o = z[:-4]
                n = "%s_%s" % (o, mode_tag(mode))
                jobs.append(dict(root=src_root, out=out_root, mode=mode, name=n, orig=o, cands=[os.path.join(md, z)],
                                 arena=None, out_world=os.path.join(out_root, ".work", mode, n),
                                 out_zip=os.path.join(od, n + ".zip"), out_arena=None,
                                 out_schematic=os.path.join(od, "Schematics", n + ".schematic"),
                                 out_preview=os.path.join(od, "Previews", n + ".png")))
    return jobs


def run_job(j):
    src = next((c for c in j["cands"] if os.path.exists(c) and _has_chunks(c)), None)
    if src is None:
        return {"mode": j["mode"], "name": j["name"], "originalName": j["orig"], "error": "source world is empty or unreadable",
                "source": [os.path.relpath(c, j["root"]).replace("\\", "/") for c in j["cands"]]}
    info = convert(src, j["name"], j["out_world"], read_arena(j["arena"]), j["out_zip"],
                   j["out_schematic"], j["out_preview"])
    info["mode"] = j["mode"]
    info["originalName"] = j["orig"]
    info["source"] = os.path.relpath(src, j["root"]).replace("\\", "/")
    if "error" in info:
        return info
    if j["arena"]:
        os.makedirs(os.path.dirname(j["out_arena"]), exist_ok=True)
        with open(j["arena"], encoding="utf-8") as f:
            text = f.read()
        # an empty display-name shows the world name in game; keep showing the map's real name
        title = j["orig"].replace("_", " ").title()
        text = re.sub(r"^display-name: *(''|\"\")? *$", "display-name: " + title, text, flags=re.M)
        with open(j["out_arena"], "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    rel = lambda p: os.path.relpath(p, j["out"]).replace("\\", "/")
    info["files"] = {k: rel(j["out_" + k]) for k in ("world", "zip", "arena", "schematic", "preview")
                     if j.get("out_" + k) and os.path.exists(j["out_" + k]) and ".work" not in j["out_" + k]}
    return info


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--tree", action="store_true", help="src is a minigames-Source style checkout")
    ap.add_argument("--arena", help="BedWars1058 arena yml (spawn + preview cut-out of the waiting lobby)")
    ap.add_argument("--name")
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    args = ap.parse_args()
    if not args.tree:
        name = args.name or os.path.splitext(os.path.basename(os.path.normpath(args.src)))[0]
        base = os.path.normpath(args.out)
        info = convert(args.src, name, base, read_arena(args.arena),
                       out_schematic=base + ".schematic", out_preview=base + ".png")
        print(json.dumps(info, indent=2))
        return
    jobs = tree_jobs(args.src, args.out)
    results = []
    with ProcessPoolExecutor(args.jobs) as ex:
        for info in ex.map(run_job, jobs):
            results.append(info)
            w = "; ".join(info.get("warnings", []))
            print("%-28s %-18s %s" % (info["mode"][:28], info["name"],
                                      info.get("error") or "%d chunks %s %s" % (info["chunks"], info["size"], w)),
                  flush=True)
    shutil.rmtree(os.path.join(args.out, ".work"), ignore_errors=True)
    renames = {r["originalName"]: r["name"] for r in results if "error" not in r and "world" not in r["files"]}
    for f in os.listdir(args.src):
        if f.endswith(".yml"):
            with open(os.path.join(args.src, f), encoding="utf-8") as fh:
                text = fh.read()
            for o, n in renames.items():
                text = re.sub(r"(world: *)%s *$" % re.escape(o), r"\g<1>" + n, text, flags=re.M)
                text = re.sub(r"^( *)%s:$" % re.escape(o), r"\g<1>" + n + ":", text, flags=re.M)
            with open(os.path.join(args.out, f), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
    with open(os.path.join(args.out, "maps.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1)
    with open(os.path.join(args.out, "MAPS.md"), "w", encoding="utf-8") as f:
        f.write(gallery(results))


def gallery(results, per_row=4):
    out = ["# Maps", "", "Top-down previews (waiting lobby cut out). Size is width x height x length in blocks.", ""]
    modes = []
    for r in results:
        if r["mode"] not in modes:
            modes.append(r["mode"])
    for m in modes:
        rows = [r for r in results if r["mode"] == m and "error" not in r]
        out += ["## " + m, "", "<table>"]
        for i in range(0, len(rows), per_row):
            out.append("<tr>")
            for r in rows[i:i + per_row]:
                img = r["files"]["preview"].replace(" ", "%20").replace("(", "%28").replace(")", "%29").replace(",", "%2C")
                if r.get("teams"):
                    teams = "%d teams<br>" % len(r["teams"])
                elif "world" in r["files"]:
                    teams = "no arena config<br>"
                else:
                    teams = ""
                out.append('<td align="center"><img src="%s" width="180"><br><b>%s</b><br><sub>%s%s</sub></td>'
                           % (img, r["name"], teams, " x ".join(map(str, r["size"]))))
            out.append("</tr>")
        out += ["</table>", ""]
        for r in results:
            if r["mode"] == m and "error" in r:
                out += ["Not included: **%s** (%s)." % (r["name"], r["error"]), ""]
    return "\n".join(out)


if __name__ == "__main__":
    main()
