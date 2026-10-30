# Implementation checklist

Legend: [x] done and tested · [~] partial (see note) · [ ] not done

## M1: Foundation
- [x] `src/` package, `pyproject.toml`, uv, Ruff, mypy, pytest
- [x] CLI with JSON envelope, stable schema, exit codes, `--help`
- [x] Four skills (`SKILL.md` + progressive references), spec-validated (`mcat skills validate`)
- [x] Skill installer: project/user scope, symlink or managed copy, conflicts, updates, broken links,
      permission failures, uninstall that never removes unrelated skills
- [x] Discovery verified: Claude Code (`claude -p`) and Codex (`codex debug prompt-input`) list all four
- [x] Credentials via OS keyring; insecure backends rejected; hidden input; no `auth show`; CI env opt-in
- [x] Modrinth search/project/version/hash lookup; User-Agent; rate limits; retries; cache; offline mode
- [x] `inspect` for .mrpack, CurseForge zip, packwiz, plain instances; inventory; injection flags
- [x] Hardened archive handling (ZIP Slip, symlinks, bombs, limits, no overwrite)

## M2: Modpack creation and mod development
- [x] CurseForge adapter (`x-api-key`, search/files/deps, restricted downloads); Modrinth-only fallback
- [~] CurseForge live test needs a key (none available in this session; unit-tested with mocks)
- [x] Deterministic resolver: exact game/loader match, backtracking on incompatibilities, pins, channels,
      excludes, cycle detection, unsatisfiable explanations with alternatives
- [x] Lockfile with hashes, provenance, reasons, sides, licenses
- [x] packwiz-format writer (native) + `.mrpack` export; `packwiz refresh` cross-check when installed
- [~] packwiz binary not installed here, so the cross-check path is untested against real packwiz
- [x] Loader catalog from Mojang / Fabric meta / NeoForge maven / Forge maven + promotions
- [x] Fabric, NeoForge, Forge templates for 26.x; **26.2 compiled with real Gradle for all three**
- [~] Other 26.x versions: template applies but needs `--allow-unverified` until built
- [ ] Templates for <= 1.21.11 (obfuscated era, different toolchains): reported unsupported
- [ ] Kotlin: blocked with an explicit message (no verified template)

## M3: Migration and debugging
- [x] Phase 1 inventory (mods, configs, KubeJS, recipes, tags, loot, worldgen, quests, assets)
- [x] Phase 2 plan: retain/upgrade/blocked/unknown with evidence; full-graph resolution of kept set;
      mechanics (FTB Quests, KubeJS, ...); editable `id_map` / `decisions`; source snapshot hash
- [x] Phase 3 transforms: id mapping (JSON/SNBT/KubeJS string literals only), vanilla renames,
      singular datapack folders (1.21), recipe result `id` (1.20.5), string ingredients (1.21.2),
      KubeJS legacy `onEvent` heads; unknown content preserved; `changes.diff`
- [x] Phase 4 build: new workspace, packwiz pack with transformed content, unmigrated jars kept aside
- [x] Phase 5 verify: structural validation, quest dependency-graph equality, unresolved references;
      overall status never "success"/"complete" while blockers exist
- [ ] Rebuilding custom source-available mods inside a migration (manual via `minecraft-moddev`)
- [ ] Runtime (server/client) launch tests: not implemented; reported as unverified
- [x] Debug analyzer: dependency, version, Java, mixin, duplicate, wrong loader, OOM, EULA, port, etc.,
      with confirmed/probable/possible confidence; pack cross-check; reversible quarantine repairs

## M4: Benchmark (Create: Astralization -> Fabric 26.2)
- [~] Real ZIP not provided; not found on Modrinth. Synthetic regression fixture built from real
      Modrinth projects (`tests/unit/test_migration.py`; live run documented in docs/verification.md)
- [x] Verified blocker: Create has no Fabric 26.2 build (Modrinth: `create` <= 1.21.1 Forge/NeoForge,
      `create-fabric` <= 1.20.1)
- [ ] Workflow comparison in Codex: blocked by the Codex account usage limit during this session
