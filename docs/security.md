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

## Known gaps

- Mod JARs are not scanned for malware (only metadata is read). Run unknown jars only in a sandbox.
- Gradle builds run with the user's privileges once `--allow-build` is given; use a container/VM for
  untrusted projects.
- Redaction catches registered secrets and common token shapes, not arbitrary personal data in logs.
