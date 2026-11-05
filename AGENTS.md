# Generic bench development and qualification

Discuss work in French. Keep the browser as the exclusive USB owner, the Python
server as REST/operation broker, and recipes in consumer repositories. Do not put
firmware assertions or OTA business logic in the bench. No physical writes without
explicit hardware consent; never infer one from permission to change source code.

## Software checks

Use Python 3.12+ and Node 22+. In `bench`:

```sh
python -m pip install -r requirements.txt -e './python[pytest]'
python -m pytest python/tests recipes -q
npm ci --ignore-scripts
npm test
npx playwright install --with-deps chromium
npm run test:browser
```

These are software simulations. General CI must never require a physical board.
Hardware pytest recipes require an explicit opt-in and remain in the consumer.
Do not merge changes without explicit user validation.

## Qualification evidence

- Preserve the first recipe versions and all historical evidence. Use a new run
  directory/version for a corrected recipe; never rewrite an old result.
- Verify the board's reference state before every batch. Record backup/restore
  policy, actual flash regions and supported transport in a validated manifest.
- Never silently correct assertions during a running test. Classify bench,
  firmware, recipe and operator failures separately, with the relevant evidence.
- Never retry hardware work or a lost operation submission automatically. Unknown
  outcomes stay indeterminate. Do not replace PendingOperation with an expected
  failure assertion just to pass a recipe.
- Close proof windows explicitly at epoch/generation interruptions. Old Board and
  cursor handles do not become valid again after recovery.
- Require campaign and per-operation programming consent, all server scopes and a
  live local grant. Remote clients cannot create, renew or revoke that grant.
- Retain artifacts needed by active or uncertain work. Keep verified restoration
  backups privately before cleaning disposable server artifacts.
- Before a successful final verdict, verify restoration, local recovery, release
  of reservations and local revocation. A simulator PASS is never hardware PASS.
- Never commit tokens, USB serial numbers, private flash dumps or campaign logs.
- The durable server needs a persistent private BENCH_STATE_DIR. Do not publish or
  discard its recovery database. A disk failure requires inspection and restart.
