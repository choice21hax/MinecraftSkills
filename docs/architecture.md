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
  pack/         build.py: resolve spec -> packwiz dir / .mrpack
  validation/   validate.py: structural, hash, lock, jar dependency checks; --online provider checks
  migration/    rules.py (documented version boundaries) · transform.py · plan.py · apply.py
  moddev/       template families, scaffold rendering, Gradle runner, error parsing
  debug/        analyze.py: rule-based log analysis, repairs (quarantine)
  security/     archive.py · credentials.py (keyring) · redact.py
  agentskills.py  installer/validator for the skills
Layer C: existing tools
  Modrinth API · CurseForge API · Gradle + Loom / ModDevGradle / ForgeGradle · packwiz (optional check)
```

## Data flow

- **Create**: spec YAML -> `resolve_spec` (LoaderCatalog checks target, Resolver selects versions) ->
  lock -> `write_packwiz` / `write_mrpack` -> `validate_path`.
- **Migrate**: `PackSource` -> `parse_pack` + `build_inventory` -> `build_plan` (identification by
  manifest ids or sha1, target candidates, full-graph resolve, content/mechanics states) -> user edits
  `id_map`/`decisions` -> `apply_plan` (snapshot check, transforms, packwiz build, validation, report).
- **Mod dev**: `plan_scaffold` (upstream versions + pinned plugin versions) -> `write_project` ->
  `run_build` (Gradle) -> parsed errors.
- **Debug**: log text -> rules -> findings ranked by confidence -> optional pack cross-check/repairs.

## Decisions and deviations from the Notion spec

| Spec item | Implementation | Reason |
| --- | --- | --- |
| Use packwiz where it supports the task | packwiz format written natively; `packwiz refresh` run as a cross-check if installed | packwiz (Go) was not installed; its file format is stable and documented. No packwiz behaviour reimplemented beyond writing metadata. |
| `providers/{modrinth,curseforge}/` packages | single modules `providers/modrinth.py`, `curseforge.py` | each is one cohesive file; packages would be empty shells |
| `loaders/{fabric,neoforge,forge}/` packages | one `loaders/__init__.py` + per-loader template families in `templates/` and `moddev/` | loader differences are data (endpoints, templates, pins), not class hierarchies |
| `mcat mod init` per spec | also `mcat mod versions`; build requires `--allow-build` | building executes Gradle plugins: explicit authorisation |
| Validation of runtime | not implemented; always reported unverified | needs an isolated sandbox + user approval |
| Extra modules | `debug/`, `pack/` | debugging and pack building are distinct responsibilities |

Version identifiers are always strings; comparisons use `resolver.versions.Version` (numeric/textual
parts, pre-releases before releases). YAML specs reject unquoted versions because `1.20` parses as the
float `1.2`.
