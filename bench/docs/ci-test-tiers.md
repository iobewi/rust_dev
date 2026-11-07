# CI test tiers

- **PR (automatic)**: `Generic hardware bench` runs Python tests excluding `@pytest.mark.slow_integration`, plus Node and browser tests. The marker applies only to long RAM-simulator end-to-end scenarios with real waits and subprocesses; quick contract, refusal, and negative tests remain in PR CI.
- **Full simulated integration (manual)**: GitHub Actions → **Generic hardware bench** → **Run workflow** (`workflow_dispatch`) runs *all* Python tests, including those marked `slow_integration`, plus Node/browser tests. Run this before major bench releases or after changing the recipes/comparator/simulator, and when investigating integration regressions.
- **Real hardware qualification**: never runs on GitHub-hosted runners. Execute the approved, version-pinned recipes on the actual ESP32/transport/LAN with explicit operator authorization and private raw evidence. Simulated PASS is not hardware qualification.

No tests or hardware recipes are deleted or weakened by this split. To reproduce locally from `bench/`:

```sh
python -m pytest python/tests recipes -m 'not slow_integration' -vv --durations=20
python -m pytest python/tests recipes -vv --durations=30
```

The full integration workflow must not be mistaken for evidence from a real board.
