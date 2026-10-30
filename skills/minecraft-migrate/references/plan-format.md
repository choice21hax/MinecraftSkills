# Migration plan format (`migration-plan.json`)

Produced by `mcat migrate plan`. Treat everything except `decisions` and `id_map` as read-only output.

| Key | Meaning |
| --- | --- |
| `source` | path, `sha256` snapshot, format, game version, loader. `apply` refuses if the source changed. |
| `target` | game version, loader, chosen loader version, required Java, upstream evidence. |
| `mods[]` | one entry per source mod: `action` (`retain`, `upgrade`, `blocked`, `unknown`), `state` (`verified`, `proposed`, `blocked`), `reason`, `evidence`, `target` version, `alternatives` (versions that *do* exist), `replacement_candidates`, `affected_namespaces`, `gaps`, `progression_critical`. |
| `added_dependencies` | new dependencies the target versions pull in. |
| `resolution_problems` | conflicts in the kept mod set. Any entry blocks `apply`. |
| `content[]` | per category (recipes, tags, kubejs, quests, configs...): action, state, namespaces of blocked mods it references. |
| `mechanics[]` | progression systems (FTB Quests, KubeJS, Game Stages...) with state. |
| `transforms` | automatic, documented transforms that will run (vanilla id renames, singular datapack folders at 1.21, recipe result `id` at 1.20.5, string ingredients at 1.21.2, KubeJS legacy `onEvent`). |
| `vanilla_id_map` | built-in renames with sources in `vanilla_id_map_sources`. |
| `id_map` | **editable**: `{"oldns:path": "newns:path"}` for identifiers of replaced mods. Only add mappings the user confirmed or that you can cite. |
| `decisions` | **editable**: `{"mods/x.jar": {"accept_missing": true}}` records that the user accepts losing a blocked mod. It is reported, not hidden. |
| `lock` | the exact target mod set (hashes, provenance) that `apply` writes; `apply` is offline. |
| `overall` | `state` is `blocked` or `proposed`; `complete` is always `false` for a plan. |

## Apply output (`--output DIR`)

- `pack/` - packwiz-format target pack with transformed configs/scripts/datapacks/quests.
- `unmigrated/` - jars that could not be migrated (kept for reference, not loaded).
- `migration-report.json` - per-mod and per-mechanic states, every change, quest integrity check,
  unresolved references to blocked mods, validation result.
- `changes.diff` - unified diff of every transformed text file, for review.

Apply status is `blocked` while required mods/mechanics are blocked, `failed` if the target pack fails
validation, otherwise `partial` - never `success`, because gameplay and quest completion are not
runtime-tested by MCAT.
