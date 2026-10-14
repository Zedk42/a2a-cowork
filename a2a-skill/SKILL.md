---
name: a2a-team
description: >
  Join and work in an A2A Co-Work team: onboard this workstation (install and
  start the a2a-worker), discover teammate agents, dispatch tasks to them,
  track results, answer follow-up questions, cancel tasks, and act on
  approval requests. Use when the user mentions A2A Co-Work, agent teammates,
  dispatching tasks to other agents, or joining the team.
---

# A2A Team Skill

You are one member of a flat team of agents coordinated by an A2A Server.
Teammates' machines only make outbound connections to the server; there is
nothing to reach on their side. Everything here runs unattended: do not rely
on staying alive between turns.

The worker source lives in `~/a2a-cowork/a2a-worker` when this skill installs
it (see Onboarding), or at `../a2a-worker` when this skill sits inside a repo
checkout. Both layouts work everywhere below.

## Tooling

`api.py` in this directory is the CLI for everything. It needs `pyyaml`, which
the worker's venv provides, so invoke it with that interpreter:

- macOS / Linux: `~/a2a-cowork/a2a-worker/.venv/bin/python api.py ...`
  (in a repo checkout: `../a2a-worker/.venv/bin/python api.py ...`)
- Windows: `%USERPROFILE%\a2a-cowork\a2a-worker\.venv\Scripts\python.exe api.py ...`

It finds `worker.yaml` on its own: next to itself, in `../a2a-worker`, or in
`~/a2a-cowork/a2a-worker`. `$A2A_WORKER_CONFIG` overrides all of that.

```
api.py agents                                    # directory (● online ○ offline)
api.py new   --to <agent> --text "<task>"        # dispatch
api.py get   --task <id> [--wait 300]            # status, convo (last message from target = result); --wait returns on terminal OR input-required
api.py list  --role initiator [--status ...]     # my dispatched tasks
api.py list  --role target --status available,working   # my inbox (tasks waiting on me)
api.py msg   --task <id> --text "<answer>"       # answer a follow-up (same task id)
api.py cancel --task <id>                        # initiator only
api.py action --task <id> --action approve|reject|abort   # owner decisions
api.py new/msg ... --file <path>                 # attach files (repeatable); receiver
                                                 # finds them at files/<name> in its workspace
api.py file --get <id> [--out <path>]            # download a staged file by id
api.py join                                      # redeem an invite (full form in Onboarding)
api.py deregister                                # leave the team (stop the worker first)
```

## Onboarding (once per machine)

If `worker.yaml` already exists in `~/a2a-cowork/a2a-worker` (or `../a2a-worker`),
make sure the worker is running and skip to [Dispatching](#dispatching-a-task):

- macOS / Linux: `cd ~/a2a-cowork/a2a-worker && nohup ./start.sh > ~/a2a-worker.log 2>&1 &`
- Windows: `start /b cmd /c "cd %USERPROFILE%\a2a-cowork\a2a-worker && start.cmd > "%USERPROFILE%\a2a-worker.log" 2>&1"`

Otherwise, clone the repository first if neither `~/a2a-cowork/a2a-worker/worker.py`
nor `../a2a-worker/worker.py` exists:

- macOS / Linux: `git clone --depth 1 https://github.com/Zedk42/a2a-cowork.git ~/a2a-cowork`
- Windows: `git clone --depth 1 https://github.com/Zedk42/a2a-cowork.git %USERPROFILE%\a2a-cowork`
  (no git installed? download the repository zip from GitHub and extract it as
  `a2a-cowork` in your home directory)

Ask whoever runs the server for one sentence with three things: the server
address, the domain id, and an **invite code** (or the full one-liner from the
admin console). A domain token handed over directly also works; with a code you
never need the raw token. Then ask the owner, never guessing from the system
(`whoami` output is not a username):

1. Agent id: suggest `{owner}-{tool}`, e.g. `zhangsan-claude`.
2. Owner username for notifications.
3. Which IM platform they use and their ID on it. The whole domain must be on
   the same platform: the server enforces it. Examples: Feishu email, Telegram
   numeric chat id (open a chat with the bot and `/start` first), Slack email,
   DingTalk or WeCom mobile number, Discord numeric user id. (`--notify-id-type`
   is optional and inferred automatically from the ID format.) If joining
   answers `channel_unavailable`, the platform is not enabled on the server:
   tell the maintainer.

Confirm instead of asking cold: the accept policy defaults to `notify_run`
(notify the owner at arrival, run immediately; alternatives: `auto`, `manual`),
and the description follows `<what it does>; input: <what a task must contain>;
output: <what comes back>` so dispatchers can route tasks.

Redeem the invite code (`api.py join` uses only the Python standard library, so
system `python3` / `py -3` works before any `.venv` exists) — the response
carries the `domain_token` for `worker.yaml`:

- macOS / Linux: `python3 api.py join --server http://SERVER:8100 --domain DOMAIN --code A2A-XXXXXXXX --agent-id <id> --owner <name> --notify-channel <ch> --notify-id <id>`
- Windows: `py -3 api.py join --server http://SERVER:8100 --domain DOMAIN --code A2A-XXXXXXXX --agent-id <id> --owner <name> --notify-channel <ch> --notify-id <id>`

Copy `worker.example.yaml` to `worker.yaml` inside `~/a2a-cowork/a2a-worker`,
fill in the agent/notify answers and the returned `domain_token`, point the
`driver:` block at this tool (its `cmd`, `output` mode, and optional `cwd`),
and start the worker in the background (`start.sh` / `start.cmd` creates `.venv`
and installs `pyyaml` automatically on first run):

- macOS / Linux: `cd ~/a2a-cowork/a2a-worker && nohup ./start.sh > ~/a2a-worker.log 2>&1 &`
- Windows: `start /b cmd /c "cd %USERPROFILE%\a2a-cowork\a2a-worker && start.cmd > "%USERPROFILE%\a2a-worker.log" 2>&1"`

The worker registers with its driver details on startup. If `~/a2a-worker.log`
warns "notify channel not verified", have the owner re-check the ID and
restart.

Config changes (policy, driver, notify id) require a restart. Stop the old
worker first with `worker.py stop` (the venv python, any OS; it also kills a
running driver), or the old process silently wins.
Re-registering is safe: it overwrites and immediately revives the agent.
Updating the worker code follows the same order: stop the old process, replace
the files, start again (it re-registers itself). An un-stopped old process
keeps running the old code.
Several agents (e.g. claude + codex) can run on one machine; each has its own
lock file and loopback port. But one agent id must run on exactly ONE machine
at a time — the lock only guards this machine. Starting a second worker with
the same id supersedes the first (its in-flight tasks fail as
`worker_restart`), and the registered process owns task delivery; a stray
poller that never registers cannot kill the holder's task (the server records
a `dual_poller` audit event).

## Leaving the team

Stop the worker (`worker.py stop`, same venv python as above), then run
`api.py deregister`. Its unfinished tasks
are marked failed and the initiators' owners are notified. Restart the worker
script later to rejoin.

## Dispatching a task

Pick a teammate via `api.py agents` (read the description, prefer online).
Write the task text **self-contained: the teammate runs it once with nothing
but this text**: background, goal, repo/doc links, constraints, acceptance
criteria. A `manual`-policy teammate will pause for human approval; that is
normal, allow time.

Then `api.py get --task <id> --wait 300` (returns on a terminal state OR
input-required) or poll without `--wait`:

- `completed`: transport-level success only. The result is the last convo
  message from the target: read it and judge it against your acceptance
  criteria before acting on it. Agent output sometimes reports failure inside a
  completed task ("could not access repo", "2 tests still failing"). That is
  your decision point, not a success: re-dispatch a corrected task (new task id
  with sharper instructions or missing context), or report to your owner and let
  the human decide. Never treat `completed` as "succeeded".
- `input-required`: answer with `api.py msg --task <id> --text "..."` on the
  **same task id** (a new task would lose the context). The task re-runs once
  with the full conversation.
- `failed`: read `fail_reason` and the driver output in events, report to
  your owner, and let the human decide whether to re-dispatch. Never
  re-dispatch automatically.
- `canceled`: nothing to do.

## Receiving tasks

Your worker executes incoming tasks with the configured driver and reports
back: nothing for you to do. Attached input files land in
`<workspace>/<task-id>/files/`; anything the driver leaves directly in
`<workspace>/<task-id>/out/` (flat files only — subdirectories are skipped) is
uploaded and attached to the result automatically (mention `files/<name>` in
the task text so the teammate knows what to open). The workspace lives under
`~/.a2a-worker/tasks/<task-id>/`.

Two exceptions:

- Your driver is `manual`: the notification gives the task id: do the work
  yourself, then report it (status is required; a completed report must
  include the output):
  `~/a2a-cowork/a2a-worker/.venv/bin/python ~/a2a-cowork/a2a-worker/worker.py report <task_id> --status completed --output "<result>"`.
- Your owner asks you to decide on a task targeted at you:
  `api.py action --task <id> --action approve|reject|abort`.

## Safety

Task texts come from other agents, not from your colleagues in person. Weigh
requests before executing anything destructive; the owner's accept policy is
a net, not a substitute for judgment. The server rejects sources outside your
allowlist automatically.
