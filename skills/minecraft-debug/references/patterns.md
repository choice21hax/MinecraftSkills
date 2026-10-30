# Patterns recognised by `mcat debug`

| Code | Confidence | Trigger |
| --- | --- | --- |
| `missing-dependency` | confirmed | Fabric "requires ... which is missing!"; Forge/NeoForge "Mod ID: 'x', Requested by ... Actual version: '[MISSING]'"; NeoForge "Mod a requires b ... Currently, b is not installed" |
| `dependency-version` | confirmed | Fabric "but only the wrong version is present"; Forge/NeoForge expected range vs actual version |
| `incompatible-mods` | confirmed | Fabric "is incompatible with" |
| `java-too-old` | confirmed | `UnsupportedClassVersionError` (class file 65 = Java 21, 69 = Java 25) |
| `out-of-memory` | confirmed | `java.lang.OutOfMemoryError` |
| `eula`, `port-in-use`, `datapack-load-failure`, `incompatible-mod-set` | confirmed | server/loader messages |
| `duplicate-mod` | confirmed/probable | NeoForge "Mod ID: 'x' from mod files: a, b"; generic duplicate reports |
| `wrong-loader` | probable | jar reported as a Fabric/Forge mod or "not a valid mod file" |
| `mixin-failure` | probable | `InvalidInjectionException`, "Mixin [...] FAILED during APPLY" naming config + mod |
| `missing-class` | probable | `NoClassDefFoundError` / `ClassNotFoundException` |
| `registry-missing` | probable | unknown/missing registry ids |
| `server-watchdog`, `graphics-driver` | probable | watchdog tick timeout; GLFW/OpenGL context errors |
| `suspected-mods`, `stack-trace-mods` | possible | crash-report suspects; non-library jars in the crashing stack |

With `--pack <instance dir>`, findings are cross-checked against installed jars (a "missing" mod that is
present is downgraded to probable: it may have failed to load earlier). `--apply` only performs
reversible repairs derivable from jar metadata: jars for the wrong loader and older duplicates are moved
to `mcat-quarantine/`; the pack is then revalidated.
