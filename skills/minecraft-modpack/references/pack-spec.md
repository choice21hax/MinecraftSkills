# Pack spec format (`mcat resolve` / `mcat pack create`)

```yaml
name: My Tech Pack
version: "1.0.0"          # pack version, string
minecraft: "26.2"          # always quote versions
loader: fabric             # fabric | neoforge | forge
loader_version: latest     # or an exact version; "latest" = newest stable from upstream metadata
channels: [release]        # allowed release channels: release, beta, alpha (default release)
include_optional: false    # also pull optional dependencies
mods:
  - sodium                 # Modrinth slug or project id (shorthand)
  - id: fabric-api
  - id: "238222"           # CurseForge project id (needs a key)
    provider: curseforge
  - id: lithium
    version: mc26.2-0.18.0 # pin a version_number or provider version id
    side: both             # client | server | both (override provider metadata)
  - id: modmenu
    optional: true         # optional in the exported pack
exclude: [some-dependency] # never pull these in (resolution fails if they are required)
```

## Lockfile (`mcat.lock.json`)

Contains, per selected mod: provider, project id, immutable version id, version number, file name,
sha1/sha512, size, download URL (or `download_allowed: false` for CurseForge files that forbid
third-party distribution), side, channel, license, and `reason` (`requested` or `dependency of X`).
Top-level sections: `restrictions` (files users must download manually from CurseForge),
`unlicensed_or_unknown` (no license metadata: CurseForge never provides one), and `provider_identity`
(`merged`: byte-identical files found on both providers, kept once with `equivalents`; `conflicts`:
same project resolved to different files; `unmatched`: CurseForge files with no Modrinth hash match).
Mods and graph edges are sorted, so the same spec and provider data always produce the same bytes.
The lockfile is the reproducible source of truth; regenerate it with `mcat resolve` rather than editing.

## Resolution failures

`blocked` results list `conflicts` (incompatible pairs, excluded required deps) and `unavailable`
(no version for the target). Each carries `alternatives` (other versions/loaders that do exist) as
evidence for a conversation with the user - not as automatic substitutions.
