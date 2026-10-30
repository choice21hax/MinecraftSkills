# Minecraft Agent Toolkit (MCAT)

MCAT lets coding agents (Claude Code, OpenAI Codex) do real Minecraft engineering: build modpacks,
develop mods, migrate packs between game versions and loaders, and debug crashes, for **Fabric,
NeoForge and Forge**.

It has two parts:
- **`mcat`**, a local, deterministic Python CLI. It handles everything that must be exact: pack
  parsing, version and loader checks, dependency resolution, hashes, lockfiles, content
  transformations, Gradle builds, sandboxed server tests and evidence-based reports.
- **Four Agent Skills** (`minecraft-modpack`, `minecraft-migrate`, `minecraft-moddev`,
  `minecraft-debug`) that teach the agent when and how to call `mcat`, and how to report honestly.

Every result carries a state: **verified** (executed and checked), **proposed**, **unverified**,
**blocked** (missing input or upstream support) or **failed**. MCAT never reports something as
verified that it did not run.

> A skill makes the agent follow a careful workflow. It does **not** guarantee that AI-written code,
> configs or migrations are correct. Review changes, and trust only what MCAT reports as verified.

Status: **1.0.0 release candidate, internal.** Not published to PyPI or any skill directory. The
license is undecided ([docs/licensing.md](docs/licensing.md)).

## Feature matrix

| Area | What works | Verified by |
| --- | --- | --- |
| Providers | Modrinth (search, versions, hashes), CurseForge (official API, your key, fingerprints, restricted files) | live tests |
| Modpacks | spec -> resolved lockfile (hashes, provenance, licenses) -> packwiz dir / `.mrpack`; inspect `.mrpack`, CurseForge zip, packwiz, instances | packwiz oracle: `packwiz refresh` is a no-op |
| Validation | structure, hashes, lock closure, jar metadata, `--online` provider checks | real 208-mod pack |
| Mod dev | templates for Fabric/NeoForge/Forge (see below); host or sandboxed Gradle builds | real builds |
| Runtime | dedicated server in a container: READY + nonce probe + clean stop; mod-loaded evidence | real servers |
| Migration | plan (retain/upgrade/replace/blocked with evidence), adapters for quests/KubeJS/datapacks/recipes, full evidence bundle, rollback | real pack (Create: Astralization) |
| Debugging | crash/log analysis with confidence levels, reversible quarantine repairs | unit + runtime |
| Client | **not automated** (needs your game account): manual checklist | - |

### Supported template combinations

Built with real Gradle **and** loaded on a real dedicated server (2026-10-30). `mcat mod matrix` shows the toolchains and evidence.

| Minecraft | Fabric | NeoForge | Forge |
| --- | --- | --- | --- |
| 26.2 | yes | yes | yes |
| 1.21.1 | yes | yes | yes |
| 1.20.1 | yes | no template | yes |

Other versions are unverified: `mod init` refuses them unless you pass `--allow-unverified`. There
are no Quilt or Kotlin templates.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
# from a built wheel (what the release check produces)
uv tool install ./minecraft_agent_toolkit-<version>-py3-none-any.whl

# or directly from a Git checkout / repository you have access to (private testing)
uv tool install git+https://github.com/choice21hax/MinecraftSkills
uv tool install /path/to/MinecraftSkills          # local clone

# upgrade / remove
uv tool install --reinstall <same source>
uv tool uninstall minecraft-agent-toolkit
```

`uv tool install minecraft-agent-toolkit` (from PyPI) is the intended future command. It does **not**
work yet because the package has not been published.

First run:

```bash
mcat doctor                                              # Python, JDKs, Gradle, packwiz, keyring, providers,
                                                         # skills, writable dirs, sandbox; never installs anything
mcat skills install --agents claude,codex --scope user  # or --scope project --project <repo>
mcat auth setup                                          # optional CurseForge key, stored in your OS keyring
mcat skills status
```

### Claude Code

`--agents claude` links the skills into `~/.claude/skills/` (user scope) or `<project>/.claude/skills/`.
Start Claude Code and ask, for example, *"make me a Fabric 1.21.1 tech pack"*: the `minecraft-modpack`
skill is selected from its description. Discovery was verified with `claude -p` (the skills appear
in the session's skill list).

### Codex

`--agents codex` links them into `~/.agents/skills/` or `<project>/.agents/skills/` (a project needs a
`.git` directory to be recognised as a project root). Discovery was verified with
`codex debug prompt-input`, which shows the skills Codex loads.

Skills are symlinks to the copy inside the installed package, so upgrading MCAT updates them. Use
`--copy` for managed copies. MCAT never overwrites an unrelated skill of the same name without `--force`.

## Using it

All commands accept `--json` and return `{schema_version, operation, status, summary, data, artifacts,
findings, blockers, warnings}`. Exit codes: `0` success, `1` failed, `2` usage, `3` partial (ran; not
everything verified), `4` blocked.

### Beginner path: ask your agent

| You say | Skill | What happens |
| --- | --- | --- |
| "Build a NeoForge 1.21.1 pack with Create and JEI" | modpack | spec -> `mcat pack create` -> validated pack + lockfile |
| "Port my pack to 1.21.1" | migrate | `migrate plan`; you decide on blocked mods; `migrate apply` |
| "Make a Forge 1.20.1 mod that adds a block" | moddev | scaffold, edit, `mod build --sandbox`, optional server test |
| "My server crashes, here is latest.log" | debug | `mcat debug` findings ranked by confidence, minimal fix |

The agent asks before doing anything that executes third-party code (builds, servers) or needs
your EULA acceptance.

### Advanced path: the CLI

**Create a modpack**

```bash
cat > pack.yaml <<'YAML'
name: Example
minecraft: "1.21.1"       # quote versions: YAML reads 1.20 as a number
loader: fabric
mods: [sodium, lithium, fabric-api]
YAML
mcat pack create pack.yaml -o ./example --mrpack ./example.mrpack
```
Expected: `success`, a packwiz directory with `mcat.lock.json` (exact versions and hashes) and an
`.mrpack`. A mod with no build for the target makes the command `blocked`, and the alternatives that
do exist are listed. MCAT never picks a mod for the wrong version.

**Validate**: `mcat validate ./example [--online]`. Without `--online` the result is `partial`,
because provider data was not re-checked.

**Develop a mod**

```bash
mcat mod versions --loader forge --minecraft 1.20.1        # upstream support + template status
mcat mod init --loader forge --minecraft 1.20.1 --mod-id demo --package com.example.demo -o ./demo --with-examples
mcat mod build ./demo --allow-build --sandbox              # official gradle image, nothing else mounted
mcat test mod ./demo/build/libs/demo-0.1.0.jar --loader forge --minecraft 1.20.1 --accept-eula
```
Expected: `mod init` returns `partial` (the code is generated but not yet built), the build
returns `success` with the jar path, and the test returns `success` with `mod-loaded` evidence.
A first sandboxed build downloads the toolchain, which takes several minutes.

**Migrate a pack**

```bash
mcat migrate plan ./old-pack.zip --minecraft 1.21.1 --loader neoforge -o plan.json
# read plan.json: mods[].state/reason/suggested_replacements, overall.blockers
mcat migrate plan ./old-pack.zip --minecraft 1.21.1 --loader neoforge --decisions mine.json -o plan.json
mcat migrate apply plan.json -o ./migrated
```
`mine.json` holds **your** decisions, keyed by the mod path from the plan:
`{"mods/x.jar": {"replace_with": "curseforge:1015100"}, "mods/y.jar": {"accept_missing": true}}`.
Suggestions from MCAT are guesses (`approved: false`) and are never applied on their own.
Expected for a large pack: `blocked`, with one reason per blocker. A migration is only `complete`
after you play-test it. `apply` writes a new directory (the source is never modified) containing the
pack, `inventory.json`, `plan.json`, `mappings.json`, `transformation-log.jsonl`, `changes.diff`,
`unresolved-references.json`, `verification-report.json` and `ROLLBACK.md`.

**Debug a crash**: `mcat debug ./logs/latest.log --pack ./instance [--apply]`. `--apply` only
quarantines files, and the change is reversible.

**Runtime testing** (opt-in): `mcat test server ./example --accept-eula` starts the server in a
container with no network, verifies it is ready with a random probe command, stops it and analyses
the logs. You must accept the [Minecraft EULA](https://aka.ms/MinecraftEULA) yourself; MCAT never
sets it. `mcat test client` only returns a manual checklist.

## Modrinth and CurseForge

Modrinth needs no account. CurseForge needs your own API key from the CurseForge console:

```bash
mcat auth set curseforge     # hidden input; stored via Secret Service / KWallet / Keychain
mcat auth test curseforge
```

No command displays a stored key. For CI only, set `MCAT_ALLOW_ENV_CREDENTIALS=1` and
`MCAT_CURSEFORGE_API_KEY`. Files whose authors disallow redistribution are reported as manual
downloads and are never fetched. When the same mod comes from both providers it is matched by hash:
identical files are merged, and different files are reported as a conflict.

## Security model (short)

- Credentials live only in the OS keyring. They are redacted from all output and never written to files.
- Archives are extracted defensively (path traversal, symlinks, zip bombs). Pack text is treated as data, never as instructions.
- Third-party code (Gradle builds, servers, mods) runs only after an explicit flag. `mcat test`
  always uses a container: no network, read-only root, all capabilities dropped, resource limits.
  `--sandbox` builds use the official Gradle image.
- Rootful Docker is root-equivalent, so MCAT prefers rootless Podman.

Details: [docs/security.md](docs/security.md). Report vulnerabilities: [SECURITY.md](SECURITY.md).

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `keyring-insecure` in `mcat doctor` | start a Secret Service (GNOME Keyring/KWallet); see docs/security.md |
| `sandbox: missing` | install Podman (preferred) or Docker; without one, `mcat test` is `blocked` |
| Fabric build: "requires at least JVM runtime version 25" | Loom needs JDK 25 even for 1.20.1: use `--sandbox` or install JDK 25 |
| `blocked: offline and not cached` | unset `MCAT_OFFLINE` once so provider data can be cached |
| skills not listed in the agent | `mcat skills status`; restart the agent; for Codex project scope, make sure `.git` exists |
| plan `resolution_problems: duplicate-project` | the same mod comes from both providers; pick one with `replace_with` |

## Known limitations

- Client launch and gameplay are not automated. Quest and KubeJS behaviour after a migration is
  unverified until someone plays it.
- No templates for NeoForge 1.20.1, Quilt or Kotlin. Other game versions are unverified.
- Config keys and resource packs are copied unchanged during migration. Modded recipe formats
  only get identifier mapping.
- Mod ids of CurseForge-only packs are inferred from slugs when no jar metadata is available. These
  inferences are labelled unverified.
- Rootless Podman has not been exercised. The GitHub workflows have only been linted and replayed
  locally, not run on GitHub.
- Mod jars are not scanned for malware.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) (setup, checks, conventions) and
[docs/versioning.md](docs/versioning.md) (semver, release policy). Documentation:
[architecture](docs/architecture.md), [security](docs/security.md),
[verification record](docs/verification.md), [checklist](docs/IMPLEMENTATION_CHECKLIST.md),
[licensing](docs/licensing.md), [changelog](CHANGELOG.md).
