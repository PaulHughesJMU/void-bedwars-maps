# void-bedwars-maps

The Hypixel Bed Wars and Duels maps from
[Trystage/minigames-Source](https://github.com/Trystage/minigames-Source), converted
into real void worlds for 1.8 servers. Browse them all in **[MAPS.md](MAPS.md)**.

Map configuration provided by TrystageBedwars. The maps themselves were built by Hypixel.

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
