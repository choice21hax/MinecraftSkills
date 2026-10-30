# Security policy

## Reporting a vulnerability

Do **not** open a public issue. Use GitHub's private vulnerability reporting for this repository
(Security tab -> "Report a vulnerability") or contact the repository owner directly. Include the MCAT
version (`mcat --version`), the command, and a minimal reproduction. Never include API keys, account
credentials or third-party modpacks you are not allowed to share.

You will get an acknowledgement when the report is read. Fixes are released as patch versions and noted
in `CHANGELOG.md`.

## Scope

In scope: credential handling (keyring, redaction), archive extraction, sandboxing of builds and servers,
command execution, prompt-injection handling of pack content, CI workflows and release artifacts.

Out of scope: vulnerabilities in Minecraft, mod loaders, mods or provider APIs themselves (report those
upstream), and the documented limits in [docs/security.md](docs/security.md#known-gaps).

## Supported versions

Only the latest released version receives fixes while MCAT is pre-1.0 / release candidate.
