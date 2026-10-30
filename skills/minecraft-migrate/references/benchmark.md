# Benchmark: Create: Astralization (1.20.1 Forge)

Measured 2026-10-30 on the user's own ZIP (never committed; keep it in `benchmarks/inputs/`, git-ignored).
Details and numbers: `docs/verification.md` (v0.4.0).

| Target | Result |
| --- | --- |
| A: Fabric 26.2 | `blocked`: 147 of 209 mods have no Fabric 26.2 build, including Create, FTB Quests and KubeJS. Correct outcome: the pack's core mechanic does not exist there. |
| B: NeoForge 1.21.1 | most feasible target: Create 6, FTB Quests and KubeJS exist; lock of 189 mods. Still `blocked` by 31 mods: 12 have same-author NeoForge projects (YUNG's) as suggestions, CC: Tweaked has only alpha builds, 18 have no build. |
| Control: Forge 1.20.1 | 202 files kept, matches `validate --online` findings. |

How to run it:

```bash
mcat inspect benchmarks/inputs/<zip> --json
mcat migrate plan benchmarks/inputs/<zip> --minecraft 1.21.1 --loader neoforge -o out/plan.json --json
# show the user each blocked mod and suggestion; write ONLY their choices to decisions.json, then:
mcat migrate plan benchmarks/inputs/<zip> --minecraft 1.21.1 --loader neoforge --decisions decisions.json -o out/plan.json --json
mcat migrate apply out/plan.json --output out/astral-neoforge-1.21.1 --json
mcat validate out/astral-neoforge-1.21.1/pack --json
```

Report which target was chosen, which decisions the user made, and that gameplay is unverified.
