# Verification record

Only what is listed here was executed. Anything not listed is unverified.

Environment (2026-10-30): Arch Linux (CachyOS kernel 7.3), Hyprland, uv 0.13.5, Python 3.12.15 /
3.13.16 / 3.14.8, Temurin JDK 25.0.4.1, Gradle 9.8.1, GNOME Keyring (Secret Service) holding a
CurseForge API key, Go 1.27.1, Docker 29.9.2 (rootful daemon). No Modrinth token. No podman, no Xvfb.

## v0.3.0

| What | Outcome |
| --- | --- |
| `uv run pytest` / ruff / mypy | 160 passed / clean / no issues in 50 files |
| `MCAT_RUN_RUNTIME_TESTS=1 uv run pytest -m runtime` | 4 passed (154 s): bare Fabric/NeoForge/Forge 26.2 READY + clean stop; broken pack caught |
| Sandbox | Docker 29.9.2 (rootful), image `eclipse-temurin:25-jre` @ sha256:ecdc0adb37bd..., Java 25.0.5 |
| `mcat test server --loader {fabric,neoforge,forge} --minecraft 26.2 --accept-eula` | all `success`: Done in 8.8 s / 7.2 s / 7.7 s; nonce echoed; exit 0; total 43 / 49 / 59 s (install 26 / 32 / 41 s) |
| `mcat test server` on an MCAT Fabric 26.2 pack (lithium, ferrite-core, appleskin, chunky, spark, modmenu) | `success`; 7 files hash-verified, modmenu skipped (client); Fabric Loader listed 49 mods |
| same pack with fabric-api removed | `failed` in 1.2 s; `runtime-missing-dependency` (confirmed) for appleskin, chunky, spark; offline `validate` now also fails it (`lock-dependency-missing`) |
| `mcat mod build` + `mcat test mod` for MCAT's 26.2 templates | Fabric, NeoForge: loaded. Forge: loaded but "Missing data pack mod:<id>" -> template fixed with MDK `pack.mcmeta`, rebuilt, re-tested: no warning |
| `mcat test client` | `blocked` by design: no account-free launch, no Xvfb; manual checklist returned |

Not verified in v0.3: rootless Podman; runtime of any 1.20.1/1.21.1 target; client rendering/gameplay;
world generation beyond the spawn area; multiplayer joins.

## v0.2.0

### Automated checks

| Command | Result |
| --- | --- |
| `uv run ruff check src tests scripts` / `ruff format --check` | pass |
| `uv run mypy` | no issues in 44 source files |
| `uv run pytest` (offline, hermetic) | 148 passed |
| CI replay: `uv sync --locked --python {3.12,3.13,3.14}` + lint + mypy + pytest with `PYTHON_KEYRING_BACKEND=fail` | 148 passed on each |
| `uv run pytest -m network` (packwiz on PATH, key from keyring) | 12 passed |
| `actionlint` on both workflows | clean |
| `uv build`; clean venv install of wheel and of sdist; `scripts/smoke_installed.py` | both pass |

### Real executions

| What | Outcome |
| --- | --- |
| packwiz built from source (`go install github.com/packwiz/packwiz@latest`, commit 170b22d, 2026-10-14) | binary in session scratch dir, not installed system-wide |
| `mcat pack create` (sodium, lithium, modmenu optional, Fabric 26.2) then `packwiz refresh` on a copy | **before fix**: packwiz added `mcat.lock.json` to the index and reordered `[versions]`. **after fix**: `diff -r` identical; `packwiz list` lists all 5 mods |
| CurseForge `test` / `search` (jei, create, sodium) | correct projects (238222, 328085, 394468) |
| CurseForge files for JEI, NeoForge 26.2 | 190 files over 4 pages, all tagged 26.2/NeoForge |
| Fingerprint of real file `create_mobile_packages-1.20.1-0.6.1.jar` (downloaded from the official `downloadUrl` to scratch) | 2303265137 == API `fileFingerprint`; sha1 equal; `/v1/fingerprints` maps it back to project 1232978 |
| Batch lookup of all 208 Astralization manifest entries | 208 mods/files returned; 11 disallow distribution (null `downloadUrl`) |
| `mcat inspect` on Create-Astralization-1.0.3.zip | 7 SNBT parse errors (`#` comments) **fixed** -> 0; 7 chapters, 202 quests, 229 dependency edges, 0 dangling |
| `mcat validate --online` on Create-Astralization-1.0.3.zip | `partial`: 201/208 confirmed for 1.20.1/Forge. Not passed: 2 files CurseForge-tagged Fabric only (f-tech-core, zfastnoise); 1 resource pack tagged 1.21.x; 2 files declare `create-fabric` (no Forge files: flagged as likely metadata errors); `sodium-options-api` and `cc-tweaked` declared required but not in the manifest (the pack ships a cc-tweaked jar in overrides that has **no** CurseForge fingerprint match, so it is reported as unverified, not as satisfied); 11 manual downloads |
| Packaging: `uv tool install <repo>` (non-editable) with isolated HOME/UV_TOOL_DIR, project skill install, `uv tool uninstall` | links dangled and were misreported as foreign (needing `--force`) **fixed**: recognised as MCAT links and updated |
| Secret scan (pattern scan of tree + full git history; byte search for the real CurseForge key across repo, `~/.cache/mcat`, session scratch: 4,241 files) | no matches |

### Not verified in v0.2
- The GitHub Actions workflows have not run on GitHub (they were linted and replayed locally; pushing
  requires authorisation).
- gitleaks/trufflehog were not run (installing them was not permitted in-session).
- Cross-provider identity is CurseForge -> Modrinth only (CurseForge has no sha1 lookup).
- CurseForge pack entries are still given placeholder `mods/cf-<project>-<file>.jar` paths offline;
  online validation labels resource packs correctly, `inspect` does not.

## v0.1.0

| Command / execution | Outcome |
| --- | --- |
| offline suite / network suite | 136 passed / 6 passed, 1 skipped (no CurseForge key then) |
| `mcat resolve` (sodium, lithium, modmenu, iris) Fabric 26.2, live Modrinth | success: 6 mods incl. fabric-api, placeholder-api |
| `mcat resolve` with `create` for Fabric 26.2 | blocked with alternatives; nothing guessed |
| `mcat pack create ... --mrpack` + `mcat validate --online` | index hashes verified; Modrinth hashes/targets confirmed |
| `mcat mod init --with-examples` + `mcat mod build --allow-build` | Fabric 26.2 (loader 0.20.1, fabric-api 0.161.0+26.2), NeoForge 26.2.0.93, Forge 26.2-65.1.5: all compiled |
| `mcat migrate plan/apply` synthetic 1.20.1 Fabric pack -> Fabric 26.2 | plan blocked (create-fabric, kubejs); apply produced workspace; quest graph identical; `create:*` reported unresolved |
| Claude Code 2.1.314 / Codex 0.165.0 discovery | all four skills listed |
| `codex exec` workflow run | blocked: Codex account usage limit |

## Still not verified (all versions)
- Any mod loading in a running game, server or client (v0.3 target).
- Templates other than 26.2; migrations to real targets with the Astralization pack (v0.4 target).
