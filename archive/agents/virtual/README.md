# Virtual Trader — Archived

This directory contained the old virtual-trader competitor infrastructure,
including strategy definitions, promotion scripts, and agent files.

## Why Archived

The virtual-trader system was the original approach for generating competing
trader variants. It has been replaced by the new prompt-sweep + nightly-pipeline
system which handles variant generation, replay-based scoring, and promotion
directly through the `src/prompt_sweep.py` and `src/nightly_replay.py` modules.

## Contents

- `scripts/virtual_promote.py` — old promotion logic (replaced by sweep → git branch flow)
- `scripts/__init__.py` — module init
- `scripts/virtual_promote_runner.py` — old runner (historical)

## Future

If the virtual-trader approach is ever needed again, the code here provides
a reference implementation. The DB tables (`trading.virtual_traders`,
`trading.virtual_configs`) still exist and can be re-enabled if needed.
