# Security

## Threat model

Modpacks, mod JARs, metadata, quest text, scripts and logs are **untrusted input**. Provider APIs are
trusted only for metadata on allow-listed hosts. Credentials are high value. Agents may be targeted by
prompt injection embedded in pack content.

## Implemented protections

| Threat | Protection | Code |
| --- | --- | --- |
| ZIP Slip / absolute paths / `..` | every member normalised and rejected on traversal; manifest paths too | `security/archive.py` `normalize_member`, `safe_join` |
| Symlink entries / symlinked dirs | symlink ZIP entries rejected; directory scans never follow symlinks | `SafeZip._validate`, `PackSource._scan_dir` |
| Decompression bombs | entry count, per-entry and total size, compression-ratio limits; reads capped even if headers lie | `ArchiveLimits`, `SafeZip.read` |
| Unsafe overwrites | outputs must be new/empty dirs (not symlinks); extraction uses exclusive create; source never written | `ensure_fresh_output`, `extract_all` |
| Source modification | plans hash the source; apply refuses if it changed; everything goes to a new workspace | `migration/plan.py` `snapshot_hash` |
| Credential storage | OS keyring only; plaintext/fail/null backends rejected; round-trip verified | `security/credentials.py` |
| Credential display/leakage | no `auth show`; secrets registered with redactor; JSON/text/log/subprocess output redacted; secret-named keys blanked | `security/redact.py`, `reporting/result.py`, `moddev.run_build` |
| Credential exfiltration | credentials attached only when the request host is the provider's own API host; stripped before redirects | `providers/http.py` |
| Untrusted redirects / SSRF | HTTPS only; host allow-list; no userinfo/odd ports; manual redirect following with re-validation | `check_url`, `HttpClient._request` |
| Malicious metadata | JSON/TOML parse errors reported, not raised; size limits; unknown dependency types ignored | `parsers/jarmeta.py`, `providers/*` |
| Prompt injection in packs | text resembling agent instructions is flagged (`untrusted-instructions`); skills tell agents to treat pack text as data | `parsers/inventory.py`, `skills/*/SKILL.md` |
| Execution of downloaded code | nothing is executed during inspect/plan/apply/validate/debug; Gradle builds require `--allow-build`; no game launches | `cli/commands_mod.py` |
| Restricted CurseForge files | never downloaded; reported as manual downloads; no bypasses | `providers/curseforge.py`, `pack/build.py` |
| Repairs | only reversible moves to `mcat-quarantine/`, only with `--apply`, then revalidation | `debug/analyze.py` |

## Keyring on Arch Linux / Hyprland

`mcat doctor` and `mcat auth setup` print this guidance when no secure backend is available. MCAT never
edits system files.

1. `sudo pacman -S gnome-keyring libsecret seahorse`
2. Start it with the session, e.g. in `~/.config/hypr/hyprland.conf`:
   `exec-once = dbus-update-activation-environment --systemd --all` and
   `exec-once = gnome-keyring-daemon --start --components=secrets`
   (or `systemctl --user enable --now gnome-keyring-daemon.socket`).
3. Confirm `echo $DBUS_SESSION_BUS_ADDRESS` is non-empty.
4. Create/unlock the `login` collection once (Seahorse). For unlock at login, add
   `auth optional pam_gnome_keyring.so` / `session optional pam_gnome_keyring.so auto_start` to your
   display manager's PAM file.
5. `mcat doctor` should report `keyring.backends.SecretService.Keyring` as secure.

## CI credentials

`MCAT_ALLOW_ENV_CREDENTIALS=1` plus `MCAT_CURSEFORGE_API_KEY` / `MCAT_MODRINTH_TOKEN`. Without the opt-in
flag the variables are ignored (and `auth status` says so). Values are never persisted.

GitHub Actions layout:
- `ci.yml` runs on every pull request and references **no secrets**; permissions are `contents: read`,
  checkout uses `persist-credentials: false`, and every third-party action is pinned to a commit SHA.
- `network.yml` is the only workflow that reads `MCAT_CURSEFORGE_API_KEY`. It triggers on `schedule`
  and `workflow_dispatch` only (never `pull_request`/`pull_request_target`), so code from forks cannot
  run with the key; the secret lives in the `providers` environment, where required reviewers can be
  configured. The key is passed only to the test step's environment.
- Uploaded artifacts are JUnit reports and built distributions from the secret-free workflow.

## Runtime sandbox (v0.3)

`mcat test` executes third-party code (loader installers, Minecraft, mods). Controls:
- Never on the host by default. With no usable container engine the result is `blocked`; host
  execution requires `--allow-host-execution` and is reported as unsandboxed.
- Container: `--network none` while the server runs (network only during the loader-install step,
  which needs the loader maven and Mojang), read-only root fs, private `/tmp` tmpfs, `--cap-drop ALL`,
  `no-new-privileges`, invoking uid/gid, memory (= swap), CPU and pid limits, `HOME=/work/home`, no
  inherited environment, only the run workspace bind-mounted. No credentials enter the container:
  CurseForge downloads happen on the host before launch.
- Installers are fetched by MCAT from official mavens and checked against the maven `.sha256`;
  pack files must match recorded hashes; files without hashes are refused.
- Docker's daemon here is rootful: docker-group membership is root-equivalent and a container escape
  would reach root. Rootless Podman is preferred automatically when installed. Containers are not a
  perfect security boundary; do not test packs from untrusted sources on machines holding secrets.
- EULA: the server is never started unless the user passes `--accept-eula`.
- Log text from mods is matched against fixed patterns only; readiness needs a nonce generated after
  the "Done" line, so a mod cannot fake readiness by printing text in advance.

## Repository hygiene (v0.2 audit)

- Pattern scan of the working tree and full git history for common token formats (CurseForge-style
  `$2a$` keys, `mrp_`, GitHub, AWS, OpenAI-style, private keys, Slack): no matches.
- Byte search for the real CurseForge key across the repo, MCAT's HTTP cache and session outputs: no
  matches. The HTTP cache stores response bodies only, never request headers.
- No binaries, jars, archives or files over 200 KB are tracked. Real modpacks and downloaded jars are
  kept outside the repository (`benchmarks/inputs/` is git-ignored).
- Offline tests cannot reach the network (autouse guard), so they cannot leak credentials either.

## Known gaps

- Mod JARs are not scanned for malware (only metadata is read). Run unknown jars only in a sandbox.
- Gradle builds run with the user's privileges once `--allow-build` is given; use a container/VM for
  untrusted projects.
- Redaction catches registered secrets and common token shapes, not arbitrary personal data in logs.
- A dedicated secret scanner (gitleaks/trufflehog) has not been run; the v0.2 audit used pattern and
  exact-key searches.
