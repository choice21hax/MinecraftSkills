## What and why

<!-- One logical change. Link the issue if there is one. -->

## How it was verified

<!-- Commands you actually ran and their results. "Not run" is an acceptable answer; a guess is not. -->

- [ ] `uv run ruff check src tests scripts && uv run ruff format --check src tests scripts`
- [ ] `uv run mypy`
- [ ] `uv run pytest` (offline): __ passed
- [ ] opt-in suites touched by this change (network / build / runtime), with output:
- [ ] `docs/verification.md`, `CHANGELOG.md` updated if behaviour changed

## Safety checklist

- [ ] No credentials, mod jars, modpacks or Minecraft assets added
- [ ] No new code path executes third-party code outside a sandbox without an explicit flag
- [ ] No compatibility claim without a recorded build/run (`compatibility.json`)
- [ ] Workflows: no secrets reachable from `pull_request`, actions pinned to SHAs
