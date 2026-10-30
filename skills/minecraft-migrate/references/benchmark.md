# Benchmark: Create: Astralization -> modern Fabric

Status as of 2026-10-30 (verified with `mcat resolve` / Modrinth API):

- Fabric, NeoForge and Forge all publish releases for Minecraft 26.2 (Fabric loader 0.20.1, Java 25).
- **Create has no Fabric build for 26.2.** `create` on Modrinth targets Forge/NeoForge up to 1.21.1;
  `create-fabric` stops at 1.20.1. A Create-centred pack therefore cannot be migrated to Fabric 26.2
  without replacing its core mechanic - `mcat migrate plan` reports this as `blocked`, which is correct.
- Create: Astralization was not found by Modrinth search; the original ZIP must be supplied.

## Running it when the ZIP is available

```bash
mkdir -p benchmarks/inputs && cp ~/Downloads/Create-Astralization*.zip benchmarks/inputs/   # never committed
mcat inspect benchmarks/inputs/<zip> --json > out/astral-inventory.json
mcat migrate plan benchmarks/inputs/<zip> --minecraft 26.2 --loader fabric -o out/astral-plan.json --json
# review blocked mods/mechanics with the user; fill id_map/decisions only with confirmed mappings
mcat migrate apply out/astral-plan.json --output out/astral-26.2-fabric --json
mcat validate out/astral-26.2-fabric/pack --json
```

Feasible alternatives to discuss: target the newest version where Create exists on the desired loader
(e.g. NeoForge 1.21.1), or keep Fabric 26.2 and accept that Create-dependent progression is blocked until
Create (or an add-on ecosystem) ships for it. Report which option was chosen and what stays unverified.
