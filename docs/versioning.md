# Versioning and release policy

MCAT follows [Semantic Versioning](https://semver.org/).

| Change | Bump |
| --- | --- |
| Breaking change to the CLI, JSON envelope (`schema_version`), exit codes, lockfile or plan format, skill names | major (minor while `0.x`) |
| New command, option, loader/version support, finding code | minor |
| Bug fix, documentation, compatibility-registry evidence refresh | patch |

`schema_version` in the JSON envelope, `lock_version` in lockfiles and `plan_version` in plans are
versioned separately and only change when their format does.

## Milestones

| Version | Theme | Status |
| --- | --- | --- |
| 0.1.0 | foundation, four skills | done (internal) |
| 0.2.0 | hardening, CI, CurseForge, cross-provider identity | done (internal) |
| 0.3.0 | runtime verification | done (internal) |
| 0.4.0 | version-aware templates, compatibility registry, real migration | done (internal) |
| 1.0.0rc1 | distribution and release engineering | candidate, see below |
| 1.0.0 | stable supported release | needs the criteria below |

## What 1.0.0 requires

1.0 is not a matter of all planned files existing. It requires:
1. the release-validation workflow passing **on GitHub** (not only replayed locally);
2. every supported combination in `mcat mod matrix` re-verified within the release cycle;
3. at least one real-world migration taken to a running server, with its decisions made by a user;
4. a licensing decision (docs/licensing.md) and agreement on how it is distributed;
5. documentation that matches observed behaviour, with experimental features labelled.

Until then, versions are release candidates (`1.0.0rcN`).

## Release procedure (owner only)

1. Update `CHANGELOG.md`, `docs/verification.md`; bump `pyproject.toml` and `src/mcat/__init__.py`.
2. `uv lock`, commit, `uv run python scripts/release_check.py out/` locally.
3. Tag `vX.Y.Z` and push the tag (needs approval). `release-validation` rebuilds the release candidate
   and uploads the artifacts, checksums and `release-report.json` as a workflow artifact.
4. Publishing (PyPI with trusted publishing, a GitHub Release, skill directories) is a separate,
   explicitly approved step. None of this is automated today.
