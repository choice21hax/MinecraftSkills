# Regression corpus

Synthetic packs (text only: manifests, quests, scripts, data). No mod jars and no third-party content.
Each case is `pack/` (an extracted `.mrpack` layout) plus `case.json`: the target, the fake provider
versions it resolves against (`[project, version_id, game_versions, loaders]`) and the expected outcome.
`tests/unit/test_corpus.py` runs plan + apply for every case and also checks invariants that hold for all
of them: the source is untouched, the full output bundle exists, quest graphs are preserved.
Add a case whenever a migration bug is fixed.
