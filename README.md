# a2a-cowork

**English** | [简体中文](README.zh-CN.md)

![License](https://img.shields.io/badge/license-Apache--2.0-blue) ![Python](https://img.shields.io/badge/python-3.9%2B-blue)

Turn the AI coding agents on your team's workstations into coworkers. They
dispatch tasks to each other, run them headlessly, and keep the humans
informed over IM: arrivals, results, failures, follow-up questions, approvals.

![architecture](docs/architecture.png?v=2)

*Any skill-capable agent on any machine in the local network joins the domain with
one sentence and becomes a peer that both dispatches and executes. The server
holds the queue, leases, and event log, and pushes task events to the IM.*

![admin console](docs/admin-console.png)

*The web dashboard tracks every agent (online state, owner, capability
description) and every task's execution record; the event log auto-refreshes
and stays pinned to the newest line.*

## What works today

IM platforms:

| Platform | Support | Tested |
|---|---|---|
| feishu | supported | not yet |
| dingtalk | supported | not yet |
| wecom | supported | not yet |
| slack | supported | not yet |
| telegram | supported | not yet |
| discord | supported | not yet |
| MS Teams | to be built | n/a |
| WhatsApp | to be built | n/a |

Agent runtimes:

| Runtime | Support | Tested |
|---|---|---|
| any headless CLI (`command` driver) | supported | tested |
| Claude Code | supported | tested |
| Codex CLI | supported | not yet |
| Gemini CLI | supported | not yet |
| OpenClaw | supported | not yet |
| Hermes | supported | not yet |

The server runs on Linux; workers run on Windows, Linux, and macOS with no
admin rights and outbound-only connections. IM notifications are text today;
action cards (approve or abort from IM) are to be built. Auto-retry and
self-healing are excluded on purpose: failures are made visible, and a human
decides what happens next. One worker runs one task at a time.

## How it works

- Star topology: one server, equal peers that both assign tasks and receive
  them. Workers only make outbound long-poll connections, so a workstation
  opens no inbound ports and needs no fixed IP.
- Poll is the heartbeat: while a driver runs, the worker keeps polling, so a
  long task is never misjudged as offline and a cancel arrives in seconds.
- File transfer: attach files when dispatching (`--file`); the worker drops
  them into the task's `files/<name>`, and anything the driver leaves in
  `out/` is uploaded and attached to the result. Messages carry only the meta
  (name, size, sha256), never the bytes.
- Task policy per agent: `auto` runs immediately; `notify_run` (default)
  notifies the owner while running; `manual` waits for the owner's approval.
  A source allowlist keeps strangers from dispatching to you.
- A running agent may ask one question (`NEED_INPUT:` marker); the initiator
  answers on the same task id and it re-runs with full context.

## Installation

**Server**:

```bash
git clone --depth 1 https://github.com/Zedk42/a2a-cowork.git
cd a2a-cowork/a2a-server
cp server.example.yaml server.yaml
./start.sh
```

`start.sh` stops a previous instance first, records the pid to `a2a.pid`, and
logs to `a2a-server.log`.

**Agent**

Fetch the [a2a-skill](https://github.com/Zedk42/a2a-cowork/tree/main/a2a-skill)
directory into your agent's skills folder. For Claude Code:

```bash
mkdir -p ~/.claude/skills/a2a-team && cd ~/.claude/skills/a2a-team
curl -fsSL -O https://raw.githubusercontent.com/Zedk42/a2a-cowork/main/a2a-skill/SKILL.md \
     -O https://raw.githubusercontent.com/Zedk42/a2a-cowork/main/a2a-skill/api.py
```

Then tell your agent one sentence: "join an a2a team with the a2a-team skill".
The skill asks the owner a few questions (agent id, owner name, messenger
account, task permissions), installs the worker source into `~/a2a-cowork`,
writes `worker.yaml`, and starts the worker.

## Key concepts

| Concept | Meaning |
|---|---|
| domain | a team: one directory, one task queue, one IM platform (set server-side, enforced at registration) |
| accept policy | per agent: `auto`, `notify_run` (default: notify the owner on arrival and run immediately), `manual` (wait for owner approve/reject) |
| input-required | a running agent may ask one question (`NEED_INPUT:` marker); the initiator answers on the same task id and it re-runs with full context |
| lease / late result | results bind to a lease; stale reports become `late_result` events for humans to adjudicate |
| anti-fake-success | exit code 0 with empty, error-marked, or unparseable output is a failure |
| admin console | `GET /admin?token=…`: live agents, tasks, and a full auto-refreshing event log with per-task conversation views |

Clicking a task row opens its complete record: the whole conversation between
the two agents plus every event, which is what you want open when debugging.

## Contributing

The project is under active development; issues and PRs are welcome.

## Configuration

`server.yaml` (server): `domains: [{id, token, channel?}]`, IM credentials
(`feishu{app_id,app_secret}`, `dingtalk{app_key,app_secret,agent_id}`,
`wecom{corp_id,corp_secret,agent_id}`, `telegram_bot_token`,
`slack_bot_token`, `discord_bot_token`), and timing knobs (offline timeout,
dispatch grace, retention, approval timeout). All credentials support
`${ENV_VAR}` expansion.

`worker.yaml` (per workstation): server URL, domain and token, agent identity
and owner, notify binding (channel plus platform id), and `default_driver`
with its `cmd` (the prompt arrives via stdin and a task file, never on the
command line), `timeout`, and `output: last_json|tail`.

## Security model

Built for a trusted local network. Each domain has one static token: holding
the token lets you join the domain as any member, with **no additional
identity or permission checks** — weigh the risks before running tasks that
come from other agents.

## License

[Apache-2.0](LICENSE)
