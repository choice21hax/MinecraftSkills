# Verification record (2026-10-30)

Environment: Arch Linux (CachyOS kernel), Hyprland session, Python 3.14 via uv 0.13.5, Temurin JDK
25.0.4.1, Gradle 9.8.1, GNOME Keyring (Secret Service). packwiz not installed. No CurseForge key.

## Automated checks

| Command | Result |
| --- | --- |
| `uv run ruff check src tests` / `ruff format --check` | pass |
| `uv run mypy` | no issues in 43 source files |
| `uv run pytest` (offline unit suite) | 136 passed |
| `uv run pytest -m network` | 6 passed, 1 skipped (CurseForge: no key) |
| `MCAT_RUN_BUILD_TESTS=1 uv run pytest -m build` | not re-run as a suite; the identical scaffold+build path was executed through the CLI (below) |

## Real executions

| What | Outcome |
| --- | --- |
| `mcat resolve` (sodium, lithium, modmenu, iris) for Fabric 26.2 against live Modrinth | success: 6 mods incl. fabric-api, placeholder-api as dependencies |
| `mcat resolve` with `create` for Fabric 26.2 | blocked, with alternatives (1.21.1/neoforge ...); nothing guessed |
| `mcat pack create ... --mrpack` + `mcat validate --online` | packwiz index hashes verified; Modrinth hashes/targets confirmed |
| `mcat mod init --with-examples` + `mcat mod build --allow-build` | **Fabric 26.2 (loader 0.20.1, fabric-api 0.161.0+26.2), NeoForge 26.2.0.93, Forge 26.2-65.1.5: all compiled**, jars produced and their metadata re-read by MCAT |
| `mcat migrate plan/apply` on a synthetic 1.20.1 Fabric pack built from real Modrinth projects (sodium, lithium, create-fabric, kubejs, fabric-api) to Fabric 26.2 | plan blocked (create-fabric, kubejs have no 26.2 Fabric build); apply produced workspace with recipe/quest transforms, quest dependency graph verified identical, `create:*` references reported unresolved |
| Claude Code 2.1.314 discovery (`claude -p` in a repo with project-scope install) | listed all four skills with descriptions |
| Codex 0.165.0 discovery (`codex debug prompt-input`) | all four skills present; absent in a control dir |
| `codex exec` workflow run | **blocked**: Codex account usage limit reached during the session |

## What is NOT verified

- Any mod loading in a running game, server or client; gameplay; rendering; quest completion.
- CurseForge live API (needs a key); packwiz's own `refresh` on MCAT output (packwiz not installed).
- Templates for game versions other than 26.2 (26.x others need `--allow-unverified`; <= 1.21.11 has no
  template).
- The Create: Astralization benchmark itself (ZIP not provided; see
  `skills/minecraft-migrate/references/benchmark.md`).
