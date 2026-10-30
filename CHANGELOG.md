# Changelog

Versions follow semver; nothing is published to a registry. See docs/verification.md for evidence.

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
