# Architecture

Three layers, as specified in "Minecraft Agent Toolkit: Architecture & Implementation Spec v0.2" (Notion).

```
Agent (Claude Code / Codex)
  └─ Layer A: skills/minecraft-{modpack,migrate,moddev,debug}/SKILL.md  (instructions only)
        └─ invokes `mcat ... --json`
Layer B: src/mcat  (deterministic engine)
  cli/          argparse front-end; one handler per command; JSON envelope
  reporting/    Result / Finding / VerificationState, exit codes, redaction on output
  models/       provider-neutral ModVersion, Dependency, FileRef, ProjectHit (versions are strings)
  providers/    http.py (allow-list, retries, cache) · modrinth.py · curseforge.py
  loaders/      upstream support + Java version per game version
  parsers/      source.py (archive|dir) · packs.py (mrpack/CF/packwiz/instance) · jarmeta.py
                inventory.py · snbt.py (FTB Quests) · jsscan.py (KubeJS)
  resolver/     spec.py · core.py (backtracking resolver, lockfile) · versions.py (Fabric/Maven ranges)
                unify.py (cross-provider identity by file hash)
  pack/         build.py: resolve spec -> packwiz dir / .mrpack
  validation/   validate.py: structural, hash, lock, jar dependency checks; --online provider checks
                runtime/: state.py (server state machine) · sandbox.py (Podman/Docker, limits)
                          provision.py (verified installers, pack files) · server.py (orchestration)
  migration/    rules.py (documented version boundaries) · adapters.py (per-content adapters)
                transform.py · plan.py · apply.py (output bundle, rollback, custom rebuild hook)
  moddev/       template families per toolchain era, compatibility.json (executed combinations),
                scaffold rendering, Gradle runner (host or sandboxed image), error parsing
  debug/        analyze.py: rule-based log analysis, repairs (quarantine)
  security/     archive.py · credentials.py (keyring) · redact.py
  agentskills.py  installer/validator for the skills
Layer C: existing tools
  Modrinth API · CurseForge API · Gradle + Loom / ModDevGradle / ForgeGradle · packwiz (oracle in CI/network tests)
CI: .github/workflows/ci.yml (offline, every PR) · network.yml (live providers, manual/scheduled)
```

## Data flow

- **Create**: spec YAML -> `resolve_spec` (LoaderCatalog checks target, Resolver selects versions,
  `unify` merges byte-identical cross-provider picks) -> lock -> `write_packwiz` / `write_mrpack` ->
  `validate_path`.
- **Migrate**: `PackSource` -> `parse_pack` + `build_inventory` -> `build_plan` (identification by
  manifest ids or sha1, concurrent target-filtered prefetch, target candidates, full-graph resolve,
  cross-provider identity, replacement suggestions, adapters, content/mechanics states) -> user edits
  `id_map` / passes `--decisions` -> `apply_plan` (snapshot check, adapters' transforms, optional
  sandboxed custom rebuild, packwiz build, validation, output bundle, immutability re-check).
- **Mod dev**: `family_for` (template family by loader + version) -> `plan_scaffold` (upstream versions +
  pinned plugin versions + per-version variables) -> `write_project` (`.mcat-project.json`) ->
  `run_build` (host Gradle, or `--sandbox`: official `gradle:<ver>-jdk<n>` image) -> parsed errors.
- **Debug**: log text -> rules -> findings ranked by confidence -> optional pack cross-check/repairs.

## Decisions and deviations from the Notion spec

| Spec item | Implementation | Reason |
| --- | --- | --- |
| Use packwiz where it supports the task | packwiz format written natively; real packwiz (built from source) is the oracle: `packwiz refresh` must be a byte-for-byte no-op | packwiz is a Go CLI that users often lack; writing its documented format keeps MCAT self-contained, and the oracle test catches drift. |
| `providers/{modrinth,curseforge}/` packages | single modules `providers/modrinth.py`, `curseforge.py` | each is one cohesive file; packages would be empty shells |
| `loaders/{fabric,neoforge,forge}/` packages | one `loaders/__init__.py` + per-loader template families in `templates/` and `moddev/` | loader differences are data (endpoints, templates, pins), not class hierarchies |
| `mcat mod init` per spec | also `mcat mod versions`; build requires `--allow-build` | building executes Gradle plugins: explicit authorisation |
| Validation of runtime | `mcat test` (v0.3): opt-in, containerised, EULA-gated; client runtime not automated | servers can be sandboxed and probed; clients need an account and a display |
| Extra modules | `debug/`, `pack/` | debugging and pack building are distinct responsibilities |

Version identifiers are always strings; comparisons use `resolver.versions.Version` (numeric/textual
parts, pre-releases before releases). YAML specs reject unquoted versions because `1.20` parses as the
float `1.2`.

## Cross-provider identity (v0.2)

A mod can be requested from Modrinth and CurseForge in one spec, or pulled in from both as a
dependency. `resolver/unify.py` runs after resolution: each CurseForge selection's sha1 (from the
CurseForge API) is looked up through Modrinth's `version_files`. If the Modrinth project is also
selected and the file is byte-identical, one entry is kept (Modrinth by default: unrestricted
downloads) and the other is recorded under `equivalents`. If the project matches but the files differ,
resolution reports `duplicate-project` with the hash evidence instead of choosing. Names and slugs are
never used. The direction is one-way because CurseForge offers no sha1 search.

CurseForge resolution fetches only the target's files (server-side `gameVersion`/`modLoaderType`
filters, paginated) and falls back to the newest 50 files only to explain alternatives; large projects
(JEI: 4,000+ files) are never fully paged.

## Templates and the compatibility registry (v0.4)

Templates are grouped by toolchain era, not by game version: `fabric/remap` (Loom remap plugin with
official Mojang mappings: 1.20.1, 1.21.1), `fabric/mc26`, `neoforge/mdg1211`, `neoforge/mc26`,
`forge/fg6` (1.20.1, reobf), `forge/fg7` (1.21.1), `forge/mc26`. Each family pins the plugin and Gradle
versions of its official example project and the JDK Gradle itself needs (`build_jdk`; Loom 1.19 needs
25 even when the mod targets Java 17). `moddev/compatibility.json` lists only combinations MCAT built
and ran; `VERIFIED_BUILDS` / `RUNTIME_VERIFIED` and `mcat mod matrix` read it.

## Migration decisions and provenance (v0.4)

Three sources of change are kept apart in every plan and in `mappings.json`:
1. **User-approved**: `id_map` (edited in the plan) and `decisions` (`--decisions`: `replace_with`,
   `accept_missing`, `rebuild_from`), stamped `provenance: user`.
2. **Automatic rules**: vanilla renames and format boundaries from `rules.py`, each citing a changelog.
3. **Machine suggestions**: `suggested_replacements` (provider search: same title ignoring loader words,
   same CurseForge author) with `approved: false`. They are never applied; a user copies one into
   decisions to use it.

The resolver never invents availability. Two metadata repairs are explicit and recorded: required
relations of resource/shader/data packs are advisory (the game does not enforce them), and a required
dependency whose CurseForge project only has files for the other loader family is listed under
`suspect_dependencies` (and validated as `dependency-metadata-suspect`) instead of being followed.
