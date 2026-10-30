# Verification record

Only what is listed here was executed. Anything not listed is unverified.

Environment (2026-10-30): Arch Linux (CachyOS kernel 7.3), Hyprland, uv 0.13.5, Python 3.12.15 /
3.13.16 / 3.14.8, Temurin JDK 25.0.4.1, Gradle 9.8.1, GNOME Keyring (Secret Service) holding a
CurseForge API key, Go 1.27.1, Docker 29.9.2 (rootful daemon). No Modrinth token. No podman, no Xvfb.

## v1.0.0rc1 (release candidate)

Same machine and tools as v0.4.0. Nothing was pushed or published.

| What | Outcome |
| --- | --- |
| Offline suite | 190 passed, 27 deselected (network/build/runtime markers) |
| ruff check, ruff format --check, mypy (52 files) | clean |
| `mcat skills validate` | success (4 skills) |
| actionlint 1.7.12 on ci.yml, network.yml, release.yml | clean |
| `scripts/release_check.py` | 22/22: two builds with `SOURCE_DATE_EPOCH` byte-identical (wheel + sdist), SHA256SUMS, version/`__version__`/Requires-Dist match, no jars/archives/binaries/>1 MB/token-shaped strings in either artifact, skills + templates + registry + notices in the wheel, clean-venv install, installed smoke test, `doctor --offline` reports every check, `mod matrix` 8 executed, inspect + validate (exit 3) of a corpus pack, offline `migrate plan` blocked (exit 4), uninstall |
| `uv tool install <checkout>` (isolated HOME/tool dir) | `mcat 1.0.0rc1`; uninstall removes the executable |
| `uv tool install git+file://<repo>` | installs the last commit (0.4.0 at the time), proving the git path; the GitHub URL itself was not tested (needs repository access from the installing machine) |
| GitHub run of release.yml / ci.yml | rc1: not run (push not yet authorised); rc2: see below |

### 1.0.0rc2 on GitHub (commit b44bca7, pushed with the owner's authorisation)

| Run | Result |
| --- | --- |
| `ci` 41134042572 (push) | success: test py3.12, py3.13, py3.14; package (wheel + sdist, clean install) |
| `release-validation` 41134068426 (manual, `network=false`) | success: release check 24/24 on the runner; live provider job skipped as designed |
| Cross-machine reproducibility | the runner's wheel and sdist are **byte-identical** to the local build (same SHA256SUMS: wheel `7cdcf323…`, sdist `6dd5dd68…`) |

The `network.yml` and opt-in live job have not run on GitHub (the `providers` environment and its
secret are not configured). The UAT below ran on rc1; rc2 changes only license files, metadata and docs.

### Clean-environment UAT (`scripts/uat.py --build --accept-eula --codex`)

Fresh HOME/XDG dirs, `uv tool install` of the rc1 wheel into an isolated tool dir, keyring disabled,
no hosted backend. EULA accepted by the user for local offline test servers.

| # | Step | Result |
| --- | --- | --- |
| 1 | install from the built wheel | passed: `mcat 1.0.0rc1` |
| 2 | install Claude Code + Codex skills | passed: 8 SKILL.md reachable |
| 3 | verify discovery | passed: 8 healthy entries; `codex debug prompt-input` lists 4/4 |
| 4 | search Modrinth | passed: 10 hits (sodium first) |
| 5 | CurseForge (optional) | passed as the no-key path: `auth test` blocked (exit 4); key entry is interactive and was not automated |
| 6 | create a minimal Fabric 1.21.1 pack | passed: lock fabric-api, lithium, modmenu, placeholder-api (dependency pulled in) |
| 7 | export `.mrpack` and re-read | passed |
| 8 | validate `--online` | passed: success |
| 9 | scaffold Forge 1.20.1 mod | passed: partial (exit 3), as designed until built |
| 10 | compile (sandboxed Gradle) | passed |
| 11 | runtime test on Forge 1.20.1 (47.4.28) server | passed with `mod-loaded` evidence, clean stop |
| 12 | broken pack (fabric-api removed) | passed: exit 1 with `lock-entry-missing`, `packwiz-missing-file` (now both required) |
| 13 | migration plan Fabric -> NeoForge 1.21.1 | passed: blocked (exit 4), 3 blocked, 1 proposed |
| 14 | interpret incomplete report | passed: complete=false, one reason per blocked mod, `apply` exit 4 |

Not covered by the UAT: a full agent conversation (model usage limits; discovery only), client launch,
rootless Podman, CurseForge with a key in the clean environment (live-tested separately in v0.2-v0.4).
The step-12 check was tightened after this run to the codes it produced; the run satisfies it.

## v0.4.0

| What | Outcome |
| --- | --- |
| `uv run pytest` / ruff / mypy | 183 passed (incl. 5 corpus cases) / clean / no issues in 51 files |
| `uv run pytest -m network -k astralization` (user's ZIP + CurseForge key) | 3 passed |
| `MCAT_RUN_BUILD_TESTS=1 MCAT_RUN_RUNTIME_TESTS=1 pytest -m "build and runtime" -k "forge-1.20.1 or neoforge-1.21.1"` | 2 passed (218 s): fresh scaffold, sandboxed build, mod loaded on a server |
| Wheel/sdist 0.4.0, clean-venv install, `scripts/smoke_installed.py`, `mcat mod matrix` from the wheel | pass |
| Exact-key scan (repo, MCAT cache, session scratch: 36,091 files) | 0 files contain the CurseForge key |
| Template builds, `mcat mod build --sandbox` (Docker 29.9.2) | fabric 1.20.1 + 1.21.1 (`gradle:9.8.2-jdk25`), neoforge 1.21.1 (`9.2.1-jdk21`), forge 1.21.1 (`9.3.1-jdk21`), forge 1.20.1 (`8.8-jdk17`): all `success` |
| First Fabric attempt | failed: "Dependency requires at least JVM runtime version 25" (Loom 1.19) -> `build_jdk` 25 |
| Two concurrent builds sharing one cache | fabric 1.20.1 failed (Gradle cache lock across containers) -> host-side lock; rebuilt OK |
| `mcat test mod <jar> --accept-eula` (fabric with a fabric-api pack) | all five `success`: READY in 30.9 s (fabric 1.20.1, Java 17), 35.9 s (forge 1.20.1, Java 17), 15.8 s (fabric 1.21.1), 11.9 s (neoforge 21.1.265), 12.6 s (forge 52.1.20); mod reported loaded; exit 0 |

### Create: Astralization benchmark (real ZIP, sha256 a847d4dc..., not committed)

Source: 1.20.1 Forge 47.4.10, 209 entries (208 CurseForge + 1 Modrinth-identified embedded jar), 7 quest
chapters / 202 quests, 18 KubeJS scripts. The ZIP's sha256 was identical before and after every run.

| Target | Mods (action/state) | Lock | Create | Quests/KubeJS | Apply |
| --- | --- | --- | --- | --- | --- |
| **A: Fabric 26.2** | 147 blocked, 61 upgrade, 1 retain | 66 mods | blocked (no Fabric 26.2 file) | blocked | `blocked`; 910 references to blocked mods; quest graph of the transformed chapter preserved |
| **B: NeoForge 1.21.1** | 173 upgrade, 31 blocked, 5 retain | 189 mods, 9 manual downloads, 1 suspect dependency | proposed (6.x for NeoForge 1.21.1) | unverified (available; needs play-test) | `blocked`; 10 unresolved refs (all `computercraft:*`); 146 identifiers in namespaces MCAT could not attribute (slug-inferred ids, unverified) |
| Control: Forge 1.20.1 | 202 retain, 6 upgrade, 1 replace | 212 mods, 11 manual downloads | verified (same file) | unverified | - |
| Forge 1.21.1 / Fabric 1.21.1 / NeoForge 26.2 | 145 / 112 / 133 blocked | none | blocked | blocked | - |

Scenario B is the most feasible target but is **not** feasible without decisions: of its 31 blockers, 12
have same-title/same-author candidates on the target (all YUNG's mods ship NeoForge 1.21.1 as separate
CurseForge projects), CC: Tweaked has only alpha builds for NeoForge 1.21.1, and 18 have no target build.
None of these suggestions was approved or applied. Nothing was launched for the migrated pack.

The control (same target) reproduces the v0.2 validation results exactly: the 6 "upgrades" are the 6 files
CurseForge tags for another loader/version, the one added dependency is `sodium-options-api`, and the 11
manual downloads match. It also exposed and verified fixes for:
- resource packs blocked for having no loader tag (39 -> 31 blockers in B);
- a pinned version overridden when reached as a dependency first (20,000-step search blow-up, no lock);
- `create-escalated` declaring a required dependency on `create-fabric` (no Forge/NeoForge files): now a
  recorded `suspect_dependencies` entry and a `dependency-metadata-suspect` warning;
- a CurseForge (1.113.1) and a Modrinth (1.120.2) CC: Tweaked in the same lock: now a `duplicate-project`
  problem with sha1 evidence. Resolved in the benchmark with a decisions file **written by the agent as a
  test fixture**, not by the pack owner.

Performance: Scenario A plan 11.3 s cold (9.2 s provider prefetch, 209 projects, 8 workers) vs minutes
before; warm cache 0.6-0.7 s; `apply` 0.5 s.

Not verified in v0.4: running any migrated pack (B has unresolved blockers); quests/KubeJS behaviour in
game; client side; Quilt; NeoForge 1.20.1 (no template); rootless Podman.

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
- Client-side loading/rendering (no account-free launch).
- Any migrated pack in a running game (v0.4 stops at blocked plans with explicit decisions pending).
