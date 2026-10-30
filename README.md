# Minecraft Agent Toolkit (MCAT)

A local, deterministic Python engine (`mcat`) plus four portable Agent Skills that let Claude Code and
OpenAI Codex build, migrate, develop and debug Minecraft modpacks and mods (Fabric, NeoForge, Forge).
The agent plans and explains; `mcat` does every deterministic step: parsing, version checks, dependency
resolution, hashing, transformations, builds and evidence-based reports.

Private/internal tool. Nothing here is published, and no third-party mods are redistributed.

## Install (local)

```bash
# from the repository root; Python 3.12+ and uv required
uv tool install --editable .        # puts `mcat` on PATH; editable keeps skill symlinks valid
mcat doctor                         # Python, Java, Gradle, packwiz, keyring, skill sources
mcat skills install --agents claude,codex --scope user      # ~/.claude/skills + ~/.agents/skills
# or per project:  mcat skills install --agents claude,codex --scope project --project /path/to/repo
mcat skills status
```

Development: `uv sync`, then `uv run pytest` (offline, hermetic), `uv run ruff check src tests scripts`,
`uv run mypy`. Opt-in suites: `uv run pytest -m network` (live providers; packwiz oracle when `packwiz`
is on PATH), `MCAT_RUN_BUILD_TESTS=1 uv run pytest -m build`. CI: `.github/workflows/ci.yml` (every PR,
no secrets) and `network.yml` (manual/scheduled).

## Skills

| Skill | Use it for |
| --- | --- |
| `minecraft-modpack` | create packs from requirements, inspect, search, resolve, export, validate |
| `minecraft-migrate` | 5-phase migration across versions/loaders with honest blocked/unverified reporting |
| `minecraft-moddev` | scaffold + build loader-specific mod projects, fix compile errors |
| `minecraft-debug` | crash reports / `latest.log` analysis, minimal fixes, reversible repairs |

Canonical sources live in `skills/`; installs are symlinks (or `--copy` managed copies) into the
documented discovery paths. Unrelated skills with the same name are never overwritten without `--force`.

## Commands

All commands accept `--json` and return the envelope `{schema_version, operation, status, summary,
data, artifacts, findings, blockers, warnings}`. Exit codes: `0` success, `1` failed, `2` usage,
`3` partial (ran; not everything verified), `4` blocked (needs input, credentials or upstream support).

```bash
mcat doctor
mcat inspect ./pack.mrpack --json                       # offline, read-only
mcat search "sodium" --minecraft 26.2 --loader fabric
mcat resolve ./pack-spec.yaml -o mcat.lock.json         # exact versions + hashes + provenance
mcat pack create ./pack-spec.yaml -o ./my-pack --mrpack ./my-pack.mrpack
mcat validate ./my-pack [--online] [--server]
mcat migrate plan ./old.mrpack --minecraft 26.2 --loader fabric -o plan.json
mcat migrate apply plan.json -o ./migrated              # offline; never touches the source
mcat mod versions --loader neoforge --minecraft 26.2
mcat mod init --loader fabric --minecraft 26.2 --mod-id mymod --package com.example.mymod -o ./mymod --with-examples
mcat mod build ./mymod --allow-build
mcat debug ./logs/latest.log --pack ./instance [--apply]
mcat test server ./my-pack --accept-eula                 # opt-in: real server in a container, no network
mcat test mod ./build/libs/mymod.jar --loader neoforge --minecraft 26.2 --accept-eula
mcat test report ~/.cache/mcat/runtime/<run>             # summary + crash analysis of a run
mcat auth setup | set curseforge | set modrinth | status | test | remove curseforge
mcat skills install|status|uninstall|validate
```

Pack spec format: [skills/minecraft-modpack/references/pack-spec.md](skills/minecraft-modpack/references/pack-spec.md).
Migration plan format: [skills/minecraft-migrate/references/plan-format.md](skills/minecraft-migrate/references/plan-format.md).

## Runtime tests (opt-in)

`mcat test` starts real dedicated servers inside a container (rootless Podman preferred, Docker
accepted). It refuses to run without `--accept-eula` (you must accept https://aka.ms/MinecraftEULA
yourself) and never falls back to running jars on your machine. Success means the server finished
loading, answered a random probe command and stopped cleanly; client gameplay is not automated
(`mcat test client` gives a manual checklist). Details: [docs/security.md](docs/security.md#runtime-sandbox-v03).

## CurseForge (optional)

MCAT works in Modrinth-only mode. To enable CurseForge, apply for an API key via the
[CurseForge developer console](https://support.curseforge.com/support/solutions/articles/9000208346),
then store it in the OS keyring with a hidden prompt:

```bash
mcat auth set curseforge     # hidden input; stored via Secret Service / KWallet / Keychain
mcat auth test curseforge
```

There is no command that displays a stored secret. For CI only, set `MCAT_ALLOW_ENV_CREDENTIALS=1` and
`MCAT_CURSEFORGE_API_KEY` from the CI secret store; values are never written to disk and are redacted.
Files whose authors disallow third-party distribution are reported as manual downloads (lockfile
`restrictions`). When a mod is selected from both providers, identical files are merged by hash and
recorded under `equivalents`; `mcat validate --online` checks CurseForge entries too.
Keyring setup on Arch/Hyprland: [docs/security.md](docs/security.md#keyring-on-arch-linux--hyprland).

## Documentation

- [docs/IMPLEMENTATION_CHECKLIST.md](docs/IMPLEMENTATION_CHECKLIST.md): milestones and status
- [docs/architecture.md](docs/architecture.md): layers, modules, data flow, spec deviations
- [docs/security.md](docs/security.md): threat model and implemented protections
- [docs/verification.md](docs/verification.md): what was actually executed and validated
