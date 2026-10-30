# Licensing and repository status

Status (2026-10-30): **unresolved, awaiting the owner's decision.** Nothing in this document has been
applied. No license file was added, no notice removed, visibility unchanged, nothing published.

## The inconsistency

| Fact | Evidence |
| --- | --- |
| The GitHub repository `choice21hax/MinecraftSkills` is **public** | `gh repo view --json visibility` -> `PUBLIC` |
| Package metadata says `Proprietary - internal use only` | `pyproject.toml` `license` |
| There is no `LICENSE` file | repository root |
| Docs called the project internal | CHANGELOG entries "(internal)" |

Being public without a license means others may read the code but have no right to use, copy or modify
it, while the "internal use only" label suggests it should not be public at all. One of the options
below should be chosen explicitly.

## Options

| | MIT | Apache-2.0 | Stay proprietary |
| --- | --- | --- | --- |
| What users may do | anything, keep the notice | anything, keep notices + NOTICE file, state changes | only what you grant |
| Patent grant | none explicit | explicit grant + patent-retaliation clause | n/a |
| Contributor clarity | low (no contribution clause) | contributions licensed under Apache-2.0 by default (section 5) | needs a CLA or employment terms |
| Compatibility | GPL-2/3 compatible | GPL-3 compatible, **not** GPL-2-only | n/a |
| Fits the ecosystem | most Fabric/Modrinth tooling (packwiz is MIT) | common for developer tooling | needs the repo to be private |
| Effort | add `LICENSE`, change `pyproject` | add `LICENSE` + `NOTICE`, change `pyproject` | make the repo private (or accept "source-visible, all rights reserved") |

**Recommendation:** if MCAT is meant to be shared with other modders or published to PyPI or skill
directories, use **Apache-2.0**. It allows the same use as MIT, adds an explicit patent grant, and
licenses contributions without a separate agreement. Choose MIT if you want the simplest possible
text. If the project should stay internal, make the repository private; until then, its status is
"all rights reserved, source visible".

Applying any option needs the owner's authorisation. It touches `pyproject.toml`, a new
`LICENSE` / `NOTICE`, the README license section and the repository visibility.

## Third-party audit

### Python runtime dependencies (not bundled: installed from their own distributions)

Collected from a clean install of the 0.4.0 wheel (`importlib.metadata`):

| Package | License |
| --- | --- |
| httpx, httpcore, idna, pycparser, SecretStorage | BSD-3-Clause |
| anyio, h11, jaraco.*, jeepney, keyring, more-itertools, PyYAML, tomli-w | MIT |
| cffi | MIT-0 |
| cryptography | Apache-2.0 OR BSD-3-Clause |
| certifi | MPL-2.0 (file-level copyleft; used unmodified, not bundled) |
| typing_extensions | PSF-2.0 |

All of these permit proprietary and open-source use. None is vendored into MCAT's wheel.

### Material shipped inside MCAT (templates)

| Template | Derived from | License | Obligation |
| --- | --- | --- | --- |
| `fabric/*` | FabricMC/fabric-example-mod | CC0-1.0 | none |
| `neoforge/*` | NeoForged MDK (`TEMPLATE_LICENSE.txt`) | MIT | keep the copyright notice: `templates/THIRD_PARTY_NOTICES.md` |
| `forge/*` | MinecraftForge MDK | Forge is LGPL-2.1; the MDK example files carry no separate license statement | **review before public redistribution**; the files are short build scripts and a minimal mod class; attribution is recorded |

Generated mod projects contain these templates, so the same notices apply to them.

### Not shipped (checked)

- No mod jars, Minecraft code, mappings or modpacks are in the repository, the wheel or the sdist.
  Gradle downloads Minecraft and the official Mojang mappings into the user's cache at build time;
  their license forbids redistributing the mappings complete and unmodified, and MCAT does not.
- The Create: Astralization benchmark ZIP is the user's own copy and stays outside the repository.
  CurseForge marks 11 of its files as non-redistributable; MCAT never downloads those.
- `tests/corpus/` is synthetic text written for MCAT.
- Real packwiz is used only as a test oracle (built in CI from source, MIT) and is not shipped.

### Provider terms

- CurseForge: files are fetched only through the official API with the user's own key and only when
  `downloadUrl` is non-null (the author allows distribution); restricted files are reported as manual
  downloads. MCAT does not ship or share an API key.
- Modrinth: public API with a descriptive User-Agent; rate limits respected.
