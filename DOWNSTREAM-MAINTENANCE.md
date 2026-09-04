# Downstream plugin gateway runtime

This branch is a temporary downstream integration branch for external Hermes
plugins that need to run beside the gateway. Its current consumer is the
standalone `orgoj/hermes-hcom-plugin` repository. Product-specific hcom code
must remain in that repository; this branch contains only generic plugin host
extensions.

## Why this branch exists

The hcom plugin currently needs four host capabilities that are not available
together on Hermes `main`:

1. gateway-owned background service lifecycle;
2. safe injection of an external message into an existing gateway route;
3. cache-safe, session-scoped plugin guidance;
4. task-local environment propagation to tool subprocesses.

It also uses interactive CLI readiness, turn, and shutdown hooks for sessions
launched by `hcom hermes`.

The clean downstream implementation is commit `ee4b211c5` on
`feat/plugin-gateway-runtime`. The earlier `refactor/hcom-plugin` branch is
historical only: its ancestry contains a direct hcom implementation followed
by its removal. Do not base an upstream PR on that history.

## Current maintenance checkpoint

On 2026-09-04 `main` merged `upstream/main` at `63279301bc` (merge commit
`5980ad3671`). An API-by-API audit confirmed that upstream does not yet replace
the route-aware prompt provider, completion-before-ack dispatch, gateway service
lifecycle, or task-local subprocess environment required by the standalone hcom plugin.

The fork's explicit policy is to minimize downstream diff and converge to
unmodified `upstream/main` as soon as upstream provides official equivalents for
these capabilities. Work continues directly on the local `main` branch.

The focused Hermes test suite passed 88 tests, the standalone plugin suite
(`orgoj/hermes-hcom-plugin`) passed 14 tests, and Ruff passed cleanly for both
repositories. The gateway service was reinstalled into the active runtime venv
and gracefully restarted (replacing PID 3033 with PID 107468). The new gateway
process successfully initialized the hcom background service and spawned listener
`kato` (PID 108205), connected to Telegram in polling mode.

Prior checkpoint (2026-08-30): merged `upstream/main` at `5cc1369fa2` (merge
commit `c1bb1bfcef`). An API-by-API audit confirmed that upstream's platform handler
generalization and hook timeout scoping did not yet replace the downstream extensions.
Focused test suite passed 87 tests, plugin suite passed 14 tests, and gateway restarted
cleanly (replacing PID 3017 with PID 788131).

Prior checkpoint (2026-08-25): merged `upstream/main` at `76e306c458` (merge
commit `f520763a9d`). Commit `237fff02e2` added exact persisted-session dispatch.
Commit `91df28a598` completed queued plugin delivery, and `f6880441fd` documented
gateway completion ownership.

## Upstream strategy

Do not open the current combined commit as a new upstream PR. Most of its API
surface overlaps active, more complete upstream work:

| Downstream need | Upstream work to watch |
| --- | --- |
| Background services | [PR #63721](https://github.com/NousResearch/hermes-agent/pull/63721) |
| Internal gateway injection | Merged [PR #84929](https://github.com/NousResearch/hermes-agent/pull/84929) (asynchronous acceptance only); [PR #83710](https://github.com/NousResearch/hermes-agent/pull/83710) remains open |
| Stable plugin prompt context | Merged [PR #81986](https://github.com/NousResearch/hermes-agent/pull/81986), but its session info does not include source/thread routing |
| Cross-surface lifecycle contract | [issue #67798](https://github.com/NousResearch/hermes-agent/issues/67798) |

The likely unique contributions are task-local subprocess environment
propagation and, if the shared lifecycle proposal does not cover the use case,
the narrow interactive CLI readiness/turn/shutdown events. Offer those as
small independent PRs only after checking the watchlist again and describing
the standalone hcom plugin as the concrete consumer.

When an upstream API lands:

1. read the merged contract and its tests rather than matching names only;
2. adapt `orgoj/hermes-hcom-plugin` to the official API;
3. run both the plugin tests and a real fresh-session Telegram E2E;
4. delete the superseded downstream implementation from this branch;
5. repeat until the plugin runs on unmodified upstream Hermes;
6. archive this branch when no downstream core patch remains.

The E2E contract is: gateway start registers the configured agent identity;
gateway stop unregisters it; a plain hcom request is visible in the active
channel; a fresh Hermes session loads the hcom skill and replies with the
configured identity without running `hcom start`; the inbound user content
contains no identity or runtime instructions.

### Fresh-session E2E gate

A deployment is valid only when all checks pass:

1. Send `/new` in the mapped messaging channel.
2. Record the new Hermes session ID and verify it differs from the prior test.
3. Send a plain hcom request whose body contains no identity, hcom command,
   lifecycle instruction, or test-specific prohibition.
4. Verify the inbound persisted user content contains only the hcom envelope
   metadata and the original message body.
5. Verify the first relevant agent action loads `hcom-agent-messaging`.
6. Verify the reply uses exactly one direct
   `hcom send ... --name <configured identity> -- '<text>'` command.
7. Verify there is no `hcom start`, `printf`, Base64, or helper encoding call.
8. Verify the visible inbound message appears in the active channel.

### Before widening the plugin API

Search current upstream issues and PRs before implementing a new extension
point. Search by the behavior and contract, not only the proposed symbol name.
At minimum check background services, gateway injection, system prompt
sections, lifecycle hooks, and subprocess environment propagation.

If overlapping work exists, compare its contract and tests first. Prefer
adapting the external plugin or contributing to that work over opening a
parallel API.

## Maintaining this branch

Repository remotes must use the conventional fork layout:

```text
origin    git@github.com:orgoj/hermes-agent.git
upstream  https://github.com/NousResearch/hermes-agent.git
```

Until Hermes ships a safe diverged-branch update workflow, do not use plain
`hermes update` from this checkout: it targets `main` and can leave the running
checkout without the downstream plugin host extensions. The tracked
`.hermes-update-blocked` marker enforces this policy for both `hermes update`
and gateway `/update`; `hermes update --check` remains read-only and allowed.
Update explicitly:

```bash
git status --short
git fetch upstream main
git merge --no-edit upstream/main
```

Resolve conflicts without dropping either upstream behavior or the generic
plugin contracts. Then validate:

```bash
source .venv/bin/activate
pytest -q \
  tests/gateway/test_plugin_runtime.py \
  tests/hermes_cli/test_cli_lifecycle_hooks.py \
  tests/hermes_cli/test_plugins.py
ruff check \
  agent/delegation_context.py cli.py gateway/platforms/base.py \
  gateway/plugin_context.py gateway/run.py hermes_cli/plugins.py \
  tests/gateway/test_plugin_runtime.py \
  tests/hermes_cli/test_cli_lifecycle_hooks.py \
  tests/hermes_cli/test_plugins.py tools/environments/local.py
```

Also run the standalone plugin suite directly from `~/projects/hermes-hcom-plugin`
without requiring writable cache directories inside that checkout:

```bash
cd ~/projects/hermes-hcom-plugin
PYTEST_ADDOPTS="-p no:cacheprovider" pytest -q
RUFF_CACHE_DIR="${TMPDIR:-/tmp}/hermes-hcom-plugin-ruff" \
  ruff check __init__.py test_plugin.py
```

Install into the service virtual environment (`/home/michael/projects/hermes-agent/venv`),
then restart the gateway and verify that the process was replaced:

```bash
old_pid="$(systemctl --user show hermes-gateway.service -p MainPID --value)"
uv pip install --python /home/michael/projects/hermes-agent/venv -e ".[all,dev]"
hermes gateway restart
hermes gateway status
new_pid="$(systemctl --user show hermes-gateway.service -p MainPID --value)"
test "$new_pid" != "$old_pid"
```

Run service-status and journal checks on the host, not inside an isolated
sandbox without access to the user's D-Bus. Confirm that the configured hcom
listener belongs to the new gateway process.

Start a new Telegram session and repeat the plain-message E2E before pushing.
A log line saying `Connecting to Telegram` is not proof of a working Telegram
connection; require a successful platform-ready/polling signal or an actual
received test message. If this requires a user-originated platform message,
stop before pushing and ask the user to send `/new` followed by the plain test
message. A passing unit suite, successful restart, and polling-ready signal do
not waive this gate. Push only after these runtime checks pass:

```bash
git push origin main
```

If the merge fails or validation regresses, do not force-push or reset away
the last known-good branch. Abort the merge, investigate in a temporary branch,
and preserve the working deployment.

## Updater work to watch

Before each maintenance merge, check whether Hermes has gained an official
workflow for diverged forks and feature branches:

- [PR #82747](https://github.com/NousResearch/hermes-agent/pull/82747) proposes
  `hermes sync-fork` for forks carrying downstream commits.
- [PR #67884](https://github.com/NousResearch/hermes-agent/pull/67884) proposes
  restoring and rebasing the user's feature branch after `hermes update`.
- [PR #72150](https://github.com/NousResearch/hermes-agent/pull/72150) proposes
  warning when an update switches away from downstream commits.

Do not change this maintenance procedure merely because one PR closes. Verify
that the accepted implementation preserves a diverged feature branch, brings
in official upstream changes, leaves the intended branch checked out, and does
not hard-reset downstream commits. Test it first with `--check`, a clean
worktree, and recoverable refs before adopting it here.
