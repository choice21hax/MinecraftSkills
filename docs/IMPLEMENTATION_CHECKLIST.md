# Implementation checklist

Legend: [x] done and tested · [~] partial (see note) · [ ] not done · [!] blocked (reason given)

Roadmap: v0.1 (M1-M4, below) -> v0.2 hardening/CI/providers -> v0.3 runtime verification ->
v0.4 versions + real migration -> v1.0 distribution. Evidence for every [x] is in
[verification.md](verification.md).

## v0.2: hardening, CI and providers
- [x] `.github/workflows/ci.yml`: PR + push to main; `contents: read`; actions pinned to commit SHAs;
      uv via `astral-sh/setup-uv` (SHA-pinned, uv 0.13.5) with cache; Python 3.12/3.13/3.14 matrix;
      `uv sync --locked`, ruff check, ruff format --check, mypy, pytest, `mcat skills validate`;
      failure-only JUnit upload (job has no secrets); actionlint-clean
- [x] Package job: wheel + sdist with SHA256SUMS, clean-venv install of each, smoke test outside the
      checkout (`scripts/smoke_installed.py`: packaged skills, project install/uninstall in a temp dir,
      JSON envelope, exit codes 1/2/4)
- [x] `.github/workflows/network.yml`: schedule + manual only (never `pull_request`), `providers`
      environment holds the CurseForge key, builds real packwiz from source as an oracle
- [~] Workflows validated locally (actionlint, local matrix replay); not yet run on GitHub (pushing
      needs the user's authorisation)
- [x] Offline tests are hermetic: an autouse guard fails any non-`network` test that opens a socket
- [x] CurseForge live: search, server-side game/loader filters, pagination (`index`/`pageSize`, 10k
      cap), batch mods/files, fingerprint identification (MurmurHash2, verified on a real file),
      restricted downloads, dependency relation types; live tests use targets taken from real results
- [x] Cross-provider identity (`resolver/unify.py`): CurseForge sha1 -> Modrinth `version_files`;
      byte-identical selections merged with `equivalents` provenance; same project / different files
      -> `duplicate-project` problem with evidence; never by name
- [x] Deterministic lockfile: mods and edges sorted; `restrictions`, `unlicensed_or_unknown`,
      `provider_identity` sections
- [x] `mcat validate --online` for CurseForge: identity, game version, loader tags, hashes, required
      dependencies (incl. "satisfiable on this target?"), project class (mods vs resource packs),
      embedded jars identified by fingerprint; lookup failures -> `unverified`
- [x] Real packwiz (source build, commit 170b22d) as oracle: `packwiz refresh` is now a byte-for-byte
      no-op on MCAT output (fixed: lockfile indexed, `versions` key order) + `packwiz list` reads it
- [x] SNBT `#` comments (found on the real Astralization pack): 7 parse errors -> 0, regression test
- [x] Packaging audit: editable, `uv tool install` (non-editable), wheel, sdist; packaged skills
      preferred over stray `skills/` dirs; dangling links from an uninstalled/moved MCAT install are
      repaired without `--force` (regression test)
- [x] `mcat auth test <provider>` without a credential -> `blocked` (exit 4)
- [x] Repository scan: no token patterns in tree or history; real CurseForge key absent from repo,
      cache and scratch outputs; no binaries or files > 200 KB
- [!] gitleaks could not be installed in this session (tool install denied); a pattern scan was used

## v0.3: runtime verification
- [x] `src/mcat/validation/runtime/` (state, sandbox, provision, server) + `mcat test server|mod|client|report`
- [x] Options: timeout, memory, CPUs, pids, workspace, cleanup, engine, image, no-pull, Java (host
      mode); automatic Java check (image's `java -version` vs Mojang manifest requirement)
- [x] Sandbox: container engine detection (Podman first), `--network none` at run time, read-only
      root, cap-drop ALL, no-new-privileges, uid/gid, memory/swap/CPU/pid limits, explicit env, only
      the workspace mounted; blocked (never host) when no engine; host only via `--allow-host-execution`
- [~] Rootless Podman path implemented but not executed (Podman not installed); Docker here is rootful
- [x] EULA: blocked without `--accept-eula`; MCAT writes `eula=true` only with it
- [x] State machine: provisioning/starting/loading_mods/loading_world/done_logged/ready/stopping/
      stopped/crashed/timeout/blocked; READY needs Done line + nonce round trip; clean = exit 0
- [x] Server smoke tests, Fabric/NeoForge/Forge 26.2 (official installers, sha256-checked): all READY
- [x] Mod test: MCAT template mods loaded on all three loaders (Fabric Loader list / FML discovery)
- [x] Broken pack detection (fabric-api removed): crash classified `runtime-missing-dependency`
- [x] Failure classification via the debug analyzer; `mcat test report` re-reads a workspace
- [x] Observability: per-phase timings, state transitions with evidence, report.json per run
- [!] Automated client launch: not offered (needs the user's Microsoft account; no Xvfb installed).
      `mcat test client` = static validation + manual checklist, reported `blocked`
- [x] Runtime tests opt-in (`-m runtime`, `MCAT_RUN_RUNTIME_TESTS=1`); excluded from CI by default

## v0.4: broader versions and real migration
- [x] Template families per toolchain era: fabric/remap (1.20.1, 1.21.1), neoforge/mdg1211, forge/fg7
      (1.21.1), forge/fg6 (1.20.1), plus the 26.x families; per-version variables (e.g. ResourceLocation)
- [x] All five legacy combinations build-verified (sandboxed official Gradle images) and
      runtime-verified (`mcat test mod`, server READY + mod listed)
- [x] Compatibility registry with provenance, dates, toolchains, images (`compatibility.json`,
      `mcat mod matrix`); absence = unverified; `not_verified` lists known gaps
- [x] `mod build --sandbox` with per-image cache and a host lock for concurrent builds
- [x] Scenario A (Fabric 26.2): blocked, every blocker with a reason; quests/KubeJS mechanics blocked
- [~] Scenario B (NeoForge 1.21.1): verified as the most feasible target (lock of 189 mods) but still
      blocked by 31 mods; 12 have replacement suggestions awaiting the user's approval; not launched
- [x] Same-target control reproduces v0.2 validation; four resolver/planner bugs found and fixed
- [x] Semantic adapters with active rules per version pair (FTB Quests, KubeJS, datapack schema, recipes,
      worldgen, config keys, resource packs); config keys and resource packs are copy-only by design
- [x] Migration outputs: inventory, plan, lockfile, transformation log with origins, diff, unresolved
      references, verification report, mappings (user vs automatic vs suggestions), ROLLBACK.md;
      source immutability re-checked after apply
- [x] User decisions (`--decisions`) separated from machine suggestions (`suggested_replacements`)
- [~] Custom-mod rebuild (`rebuild_from` + `--allow-build`): gating tested; not executed on a real
      custom mod from a real pack (the benchmark pack has none with available source)
- [x] Regression corpus (`tests/corpus/`, synthetic text-only, 5 cases)
- [x] Performance: bounded concurrent prefetch + batch lookups; per-phase timings in plans
- [ ] NeoForge 1.20.1 and Quilt templates (listed as not verified)

## v1.0: distribution
- [ ] Licensing recommendation, reproducible packaging, `doctor` expansion, docs, release validation

## v0.1 milestones (M1-M4)

### M1: Foundation
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

### M2: Modpack creation and mod development
- [x] CurseForge adapter (`x-api-key`, search/files/deps, restricted downloads); Modrinth-only fallback
- [x] CurseForge live test (v0.2, key in keyring)
- [x] Deterministic resolver: exact game/loader match, backtracking on incompatibilities, pins, channels,
      excludes, cycle detection, unsatisfiable explanations with alternatives
- [x] Lockfile with hashes, provenance, reasons, sides, licenses
- [x] packwiz-format writer (native) + `.mrpack` export; real packwiz cross-check (v0.2)
- [x] Loader catalog from Mojang / Fabric meta / NeoForge maven / Forge maven + promotions
- [x] Fabric, NeoForge, Forge templates for 26.x; **26.2 compiled with real Gradle for all three**
- [~] Other 26.x versions: template applies but needs `--allow-unverified` until built
- [x] Templates for 1.20.1 and 1.21.1 (v0.4); other pre-26 versions still need `--allow-unverified`
- [ ] Kotlin: blocked with an explicit message (no verified template)

### M3: Migration and debugging
- [x] Phase 1-5 migration workflow (see v0.1 notes in verification.md)
- [~] Rebuilding custom source-available mods inside a migration (v0.4: hook + gating, see above)
- [x] Runtime server launch tests (v0.3); client: manual checklist only
- [x] Debug analyzer with confidence levels, pack cross-check, reversible quarantine repairs

### M4: Benchmark (Create: Astralization -> Fabric 26.2)
- [x] Real ZIP now available locally (not committed); `inspect` and `validate --online` run on it (v0.2)
- [x] Verified blocker: Create has no Fabric 26.2 build
- [x] Scenario A/B plan + apply runs on the real pack (v0.4; see verification.md)
- [ ] Workflow comparison in Codex: blocked by the Codex account usage limit (v0.1 session)
