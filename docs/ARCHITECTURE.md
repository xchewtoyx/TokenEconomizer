# Architecture

The three mechanisms (see the [main README](../README.md)) know neither the agent, the harness
nor the task domain. Everything specific to those enters through a few small interfaces in the
standard-library-only package `tokeneconomizer`:

- `tokeneconomizer.trace` is the one place a saved session log is read (`load_trajectory`,
  `call_cwds`, `tool_usage`, `parse_catalog`), with one module per agent's format. A new agent
  needs a reader here and nothing else (OpenClaw's plugin writes its log in Pi's format, so it
  needs none).
- `tokeneconomizer.ledger` and `tokeneconomizer.pricing` are the one definition of cost.
  Mechanisms record their per-task overhead (in the plugin, a session counts as one task); the
  plugin's session ledger and the experiments' aggregation read it back through the same modules,
  so recorded and reported cost cannot drift.
- `tokeneconomizer.domain.DomainAdapter` is the interface to the task domain: reading a session,
  the tools pruning protects or excludes, a trajectory's loop signals, the task text for the
  optional judge, and the micro-skill vocabulary. A new domain is a new adapter, with no change
  to a mechanism. The plugin's adapter is `tokeneconomizer.plugin.adapter.LiveAdapter`; the
  benchmark adapters (SWE-Bench, Web-Bench, CredData, ReposVul) are in
  `experiments/te_bench/adapters/`.
- `tokeneconomizer.stage.Stage` is the set of hooks a mechanism offers whoever runs it:
  `before_task`, `build_tools`, `run_kwargs` and `after_task`.

Two callers run the stages, and the mechanisms import neither:

- **The plugin** (`tokeneconomizer.plugin`) runs them inside a person's sessions through each
  agent's hooks: `PrefixStage` and `ToolingStage` over each finished session, and the loop guard
  (`loopguard.TurnGuard`) one tool call at a time. What happens at each hook, and what was
  verified on each agent, is in [`PLUGIN.md`](PLUGIN.md).
- **The experiments** (`experiments/te_bench`) run them over benchmark task streams: the
  `Economizer` facade wires the three stages around one adapter, and
  `te_bench.runner.AgentRunner` is the interface to a task-execution harness (Docker, an agent
  CLI, a grader), with `experiments/te_harness` as the implementation the paper used. The
  benchmark-only controllers -- `SpiralStage`, the streamed `NudgeGuard` and the Codex finalize
  policy -- live there too. See [`experiments/README.md`](../experiments/README.md).

`tests/test_plugin_package.py` pins both boundaries: the mechanisms never import
`tokeneconomizer.plugin`, and nothing under `tokeneconomizer/` imports `experiments/`.
