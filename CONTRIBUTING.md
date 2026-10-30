# Contributing to MCAT

MCAT is currently developed privately by its owner and invited collaborators. The repository is
visible, but **no license has been granted and outside contributions are not being accepted** until
the licensing decision in [docs/licensing.md](docs/licensing.md) is made. This guide is for invited
collaborators.

## Setup

```bash
git clone <repo> && cd MinecraftSkills
uv sync --locked                      # Python 3.12+; installs dev tools from uv.lock
uv run mcat doctor --offline          # what your machine can and cannot do
```

Optional: a JDK (or Docker/Podman for `--sandbox` builds and `mcat test`), packwiz, a CurseForge
API key in the OS keyring (`uv run mcat auth set curseforge`).

## Checks (all must pass before a commit)

```bash
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run mypy
uv run pytest                          # offline and hermetic: any network access fails a test
uv run mcat skills validate
```

Opt-in suites (run the ones your change touches and paste the result in the PR):

| Suite | Command | Needs |
| --- | --- | --- |
| live providers | `uv run pytest -m network` | network; CurseForge key for `-k curseforge` |
| Gradle builds | `MCAT_RUN_BUILD_TESTS=1 uv run pytest -m build` | JDK or container engine, minutes |
| servers | `MCAT_RUN_RUNTIME_TESTS=1 uv run pytest -m runtime` | container engine; **you** accept the Minecraft EULA |
| registry re-verification | both env vars, `-m "build and runtime"` | as above |
| release candidate | `uv run python scripts/release_check.py out/` | uv |

## Conventions

- Python 3.12, Ruff (line length 120), mypy strict settings from `pyproject.toml`; match the style of
  the surrounding code. Comments explain *why*, not what.
- Every CLI command returns the JSON envelope (`--json`); states are `verified / proposed /
  unverified / blocked / failed`. Never report something as verified without having executed it.
- A compatibility claim goes into `src/mcat/moddev/compatibility.json` only after a real build and
  server run, with date, toolchain and evidence; add the run to `docs/verification.md`.
- Pack content, mod metadata and logs are untrusted data. No third-party code runs outside a sandbox
  without an explicit flag.
- Never commit mod jars, modpacks, Minecraft assets or credentials. Test packs are synthetic
  (`tests/corpus/`); add a corpus case for every migration bug you fix.
- Do not weaken a test to make it pass.

## Commits and pull requests

- One logical change per commit; imperative subject (`Add ...`, `Fix ...`) of at most 72 characters,
  body explaining why and how it was verified.
- Update `CHANGELOG.md`, `docs/verification.md` (commands actually run and their results) and the
  checklist when behaviour changes.
- Pull requests use the template; CI (`ci.yml`) runs without secrets.

## Releases

See [docs/versioning.md](docs/versioning.md). Publishing anything (PyPI, GitHub Releases, skill
directories) requires the owner's explicit approval.

## Security

Report vulnerabilities privately: see [SECURITY.md](SECURITY.md).
