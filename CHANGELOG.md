# Changelog

Versions follow semver; nothing is published to a registry. See docs/verification.md for evidence.

## 0.4.0 (2026-10-30, internal)

- Version-aware templates: Fabric 1.20.1/1.21.1 (fabric-loom-remap, Mojang mappings), NeoForge 1.21.1
  (ModDevGradle 2.0.148), Forge 1.21.1 (ForgeGradle 7) and Forge 1.20.1 (ForgeGradle 6, reobf), taken
  from the official example mods/MDKs. All five built in a sandboxed official Gradle image and loaded on
  a real server (`mcat test mod`). Fabric Loom 1.19 needs JVM 25 to run Gradle even for 1.20.1.
- Compatibility registry `moddev/compatibility.json` (+ `mcat mod matrix`): every combination MCAT built
  and ran, with dates, toolchains, images and evidence. `VERIFIED_BUILDS` / `RUNTIME_VERIFIED` derive
  from it; anything absent is unverified.
- `mcat mod build --sandbox`: Gradle in `gradle:<ver>-jdk<n>` with a per-image cache; builds sharing a
  cache are serialised with a host lock (two concurrent containers corrupted the cache otherwise).
- Migration planning (real pack: Create: Astralization, 209 entries):
  - target-filtered CurseForge fetch + bounded concurrent prefetch (8 workers, batch mod lookup):
    ~10 s cold, < 1 s warm (was minutes); per-phase timings in the plan;
  - resource/shader/data packs match on game version only (were false blockers);
  - pins hold wherever a project is first reached (fixed a 20,000-step backtracking blow-up);
  - a resource pack's "required" relations are advisory; a required dependency whose project has only
    other-loader-family files is recorded in `suspect_dependencies`, not followed and not hidden;
  - cross-provider identity check in plans (caught a real CurseForge/Modrinth CC: Tweaked duplicate);
  - minimal change: the pack's own file is kept when it already supports the target;
  - blocked reasons distinguish "no build" from "only alpha builds (use --allow-alpha)";
  - `suggested_replacements` from provider search (same title ignoring loader words / same author),
    never applied; user decisions (`--decisions`: `replace_with`, `accept_missing`, `rebuild_from`)
    carry provenance "user";
  - semantic adapters (`migration/adapters.py`: ftb-quests, kubejs, datapack-schema, recipes, worldgen,
    config-keys, resource-packs) listed in the plan with the exact rules active for the version pair.
- `migrate apply` writes the full record: inventory, plan, lockfile, transformation log (each change with
  its origin: user id_map, cited vanilla rule or transform rule), diff, unresolved references,
  verification report, mappings (user-approved vs automatic vs unapplied machine suggestions) and
  ROLLBACK.md; it re-checks that the source pack is unchanged afterwards. Optional custom-mod rebuild
  (`rebuild_from` + `--allow-build`, sandboxed).
- Regression corpus `tests/corpus/` (synthetic, text-only) with a plan+apply test per case.
- Tests: 183 offline; opt-in network benchmark (needs the user's ZIP) and registry re-verification.

## 0.3.0 (2026-10-30, internal)

- `mcat test server|mod|client|report`: opt-in runtime verification. Servers run in a container
  (rootless Podman preferred, Docker accepted with a root-equivalence warning) with no network, memory/
  CPU/pid limits, read-only root, all capabilities dropped and only the run workspace mounted. No host
  fallback without `--allow-host-execution`; no server start without `--accept-eula`.
- Readiness = vanilla "Done" line + a random-nonce command round trip; clean = `stop` + exit 0.
  Failures are classified with the `minecraft-debug` analyzer (`runtime-*` findings).
- Official installers verified against maven `.sha256`; pack files hash-verified, downloaded with
  bounded concurrency; restricted CurseForge files block the run instead of being fetched.
- Real results: Fabric, NeoForge, Forge 26.2 servers READY; MCAT template mods load on all three; a
  deliberately broken pack (fabric-api removed) is caught with a confirmed missing-dependency finding.
- Fix found at runtime: Forge 26.2 template lacked `pack.mcmeta` ("Missing data pack"); added from
  the official 65.1.5 MDK. `RUNTIME_VERIFIED` records template combos proven on a server.
- `validate` now checks the lockfile's dependency closure offline (`lock-dependency-missing`).
- Tests: 160 offline; opt-in `runtime` marker (`MCAT_RUN_RUNTIME_TESTS=1`): 4 real container runs.

## 0.2.0 (2026-10-30, internal)

- CI: `ci.yml` (PR + main, least privilege, SHA-pinned actions, uv cache, Python 3.12-3.14, lint, types,
  tests, skills validation, wheel/sdist clean-install smoke test with checksums) and `network.yml`
  (manual/scheduled only, environment-scoped CurseForge key, real packwiz oracle).
- CurseForge: pagination, server-side target filters, batch mod/file lookups, fingerprint
  identification, single-file lookup without a project id; live tests on real targets.
- Resolver: cross-provider identity by file hash (merge identical files, report same-project
  conflicts), deterministic ordering, `restrictions` / `unlicensed_or_unknown` / `provider_identity`
  in the lockfile; `mcat resolve` reports manual downloads.
- `validate --online` checks CurseForge entries (identity, target, hashes, dependencies, project
  class, embedded-jar fingerprints); failed lookups are `unverified`.
- packwiz writer: byte-identical with `packwiz refresh` (`.packwizignore` for the lockfile, sorted
  `[versions]`).
- Fixes: SNBT `#` comments; packaged skills preferred over stray `skills/` folders; dangling links from
  another MCAT install are repaired without `--force`; `auth test <provider>` without a key is `blocked`.
- Tests: hermetic offline suite (network access fails the test); 148 offline, 12 network.

## 0.1.0 (2026-10-30, unreleased, internal)

- M1: package, CLI + JSON envelope, four Agent Skills, cross-agent installer, keyring credentials,
  Modrinth adapter, pack inspection (mrpack / CurseForge / packwiz / instance), hardened archives.
- M2: CurseForge adapter, deterministic resolver + lockfile, packwiz/mrpack export, validation,
  Fabric/NeoForge/Forge 26.x templates (26.2 build-verified for all three).
- M3: migration plan/apply with documented transforms, quest-graph integrity checks, honest reporting;
  crash/log analyzer with confidence levels and reversible repairs.
- M4: benchmark documented; blocked on missing input ZIP and Create's lack of a Fabric 26.2 build.
