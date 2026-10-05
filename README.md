# void-bedwars-maps

The Hypixel Bed Wars and Duels maps from
[Trystage/minigames-Source](https://github.com/Trystage/minigames-Source), converted
into real void worlds for 1.8 servers. Browse them all in **[MAPS.md](MAPS.md)**.

Map configuration provided by TrystageBedwars. The maps themselves were built by Hypixel.

## Why

The original worlds are World Downloader saves. Each save holds the map, a ring of
empty chunks around it (the downloader's view distance), and a level.dat that says
`default`. 43 of them have no level.dat at all. When a server loads one, it generates
normal terrain beyond the empty ring: hills, oceans and caves a few hundred blocks from
the islands.

## What changed

- **Only the map is kept.** Every connected group of non-empty chunks stays; the empty
  ring is dropped (407k empty chunks removed, 11.7k map chunks kept). A group is only
  dropped if it runs into unsaved space *and* has a bedrock floor (generated terrain).
  None of these maps had any.
- **The world is void.** level.dat uses the flat generator with a single air layer
  (`2;0;1;`), so every chunk the server creates past the map is empty. Spawn is set to
  the waiting lobby from the arena config. Time is frozen at noon and weather stays
  clear. Fire spread, mob spawning and random ticks are off.
- **Downloader leftovers are gone.** That covers playerdata, stats, the Nether and End
  folders (some held chunks of Hypixel's lobby), session.lock, uid.dat (a fresh one is
  generated, so two copies of a map can load side by side), dropped items and arrows.
- **1.8 fixes.** A few chunks had been re-saved by a 1.11 server, which renames chests
  to `minecraft:chest`. A 1.8 server skips IDs it doesn't know, so those chests would
  lose their data. They are renamed back.
- **Same layout as Trystage's repo.** `Arenas/` (BedWars1058 configs, unchanged),
  `World/`, and `Cache/` (BedWars1058 restore zips, rebuilt from the cleaned worlds).
  Duels and Bedfight stay as zips.
- **New:** `Schematics/` (MCEdit `.schematic` per map), `Previews/` (top-down PNGs),
  and `maps.json` (bounds, size, spawn and teams for every map).

The map data went from 907 MB to 71 MB.

## Using the maps

**BedWars1058:** copy `Arenas/*.yml` into `plugins/BedWars1058/Arenas/` and the world
folders into your server root, the same as with the original repo.

**Any world loader** (Multiverse `/mv import <name> normal`, your own plugin): the
level.dat makes the world void, so nothing else is needed. One exception: a plugin that
passes its own chunk generator overrides level.dat. If yours does, make that generator
void.

**WorldEdit / FAWE:** put the `.schematic` in `plugins/WorldEdit/schematics/`, run
`//schem load <name>`, then:
- `//paste -o` pastes it at its original coordinates, which the arena configs use.
- Plain `//paste` centers the map on you, with its lowest block at your feet.

Signs, chests, heads, banners and other tile entities are included. Item frames are not.

## Not included

- **rise** (Bedwars4v4v4v4). The source is broken: the World folder is empty and the
  Cache zip was cut off mid-upload, so only half the map survives.

`chained` and `lobby` (Bedwars4v4v4v4) have worlds but no arena config, same as upstream.

## Converting your own downloads

```
pip install numpy pillow pyyaml
python tools/voidify.py path/to/world out/world --arena path/to/arena.yml
python tools/voidify.py --tree path/to/minigames-Source .
```

The first command converts one world (a folder or a zip) and writes
`out/world.schematic` and `out/world.png` next to it. The second rebuilds this whole
repo from a minigames-Source checkout. Pillow (previews) and PyYAML (arena configs)
are optional.

## Tested

Tested on a 1.8.8 Spigot fork with the converted archway. The server generated 590 new
chunks around the map, all void. The original download on the same server generated
350 terrain chunks. Every schematic and Cache zip was re-read and checked against its
world.

Newer servers should upgrade these 1.8 worlds on load, but that hasn't been tested.
