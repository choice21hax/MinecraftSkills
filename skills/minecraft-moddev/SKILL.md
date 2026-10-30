---
name: minecraft-moddev
description: Scaffold, modify and build Minecraft Java mods for Fabric, NeoForge or Forge with version-correct Gradle toolchains via the `mcat` CLI, add blocks/items/registrations, and diagnose compiler or Gradle failures. Use when the user wants to start a mod project, add content to a mod, port mod code between loaders, or fix a broken mod build.
license: Proprietary (internal)
compatibility: Requires the `mcat` CLI, a JDK matching the target game (e.g. Java 25 for Minecraft 26.x) and Gradle; builds download official loader toolchains from their Maven repositories.
metadata:
  toolkit: minecraft-agent-toolkit
  version: "0.1"
---

# Minecraft mod development

Each loader has its own API and build plugin. Never write one generic mod and assume it works on all
three loaders.

## Workflow

1. **Check support**: `mcat mod versions --loader <loader> --minecraft <ver> --json` reports upstream
   loader availability, the Java version the game needs, and whether MCAT has a template that was
   actually built for that combination (`template.verified`). If none exists, say so; don't fake it.
2. **Scaffold**: `mcat mod init --loader <loader> --minecraft <ver> --mod-id <id> --package <pkg>
   --output ./my-mod --json` (add `--with-examples` for an example block + item registration).
3. **Edit code** using the loader-specific patterns in
   [references/loader-apis.md](references/loader-apis.md). Keep mod id, package and metadata files
   consistent (`fabric.mod.json`, `META-INF/neoforge.mods.toml`, `META-INF/mods.toml`).
4. **Build**: `mcat mod build ./my-mod --json`. Gradle downloads the official toolchain and
   Minecraft; this executes the loader's Gradle plugin, so the first build in a session needs the
   user's approval (`--allow-build`). On failure, the result contains parsed compiler errors
   (`file:line`) - fix the root cause, rebuild, repeat.

## Truthfulness

- A successful Gradle build proves compilation only - not that the mod loads or works in game.
- Runtime checks (`gradle runServer`/client) are opt-in, run only with user permission, and a server
  starting does not certify client rendering or gameplay.
- Kotlin is only offered where MCAT has verified a template; otherwise use Java.
- Never run build scripts from downloaded third-party projects without asking the user first.
