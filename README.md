# Chisel

Chisel is a Python service that polls configurable sources for task prompts,
invokes the Claude Code CLI agent against one or more locally-cloned git repositories, and
reports outcomes via HTTP callback or Discord DM.

It is designed for small, well-scoped, single-commit code changes. The agent reads the
codebase, makes a fix, and writes structured output files that the orchestrator uses to
commit, push, and open a pull request. If the task is out of scope or the agent cannot
identify a clean solution, it writes an abort file and the job is declined with no git
operations performed.

Chisel is generic: it knows nothing about exceptions, Discord channels, or fingerprints.
Any system that exposes a compatible polling endpoint can use it. The first client is a
Minecraft server exception tracker.

---

## Design Overview

- **No database.** All state is in-memory. The ops channel, log files, and git history are
  the durable record. State is lost on restart; deduplication is best-effort.
- **Sequential processing.** One job runs at a time. Discord `/chisel` jobs queue up.
- **No inbound connectivity required.** Chisel is an HTTP client only. It polls configured
  sources for work and POSTs results back when done. No HTTP server is exposed.
- **Orchestrator owns git.** The Claude agent edits files only. All `git add`, `git commit`,
  `git push`, and `gh pr create` operations are performed by the orchestrator after the
  agent exits cleanly. The agent is explicitly instructed not to run git commands.
- **`agent_context.md` is the control surface.** Agent behavior (abort conditions,
  complexity budget, output format requirements, project conventions) is controlled entirely
  by this file. No code changes are needed to tune agent behavior.
- **Abort mechanism.** The agent writes `CHISEL_ABORT.txt` to its working directory to
  signal an abort. The orchestrator detects this on exit and sends a `declined` callback.
  A Discord `/abort` command terminates the subprocess immediately via `proc.terminate()`.

---

## Repository Structure

```
chisel/
+-- main.py               # Entry point: startup, shutdown, signal handling
+-- bot.py                # ChiselBot (discord.py)
+-- agent_context.md      # Agent instructions (version-controlled, operator-editable)
+-- config.yml.example    # Documented config reference; copy/mount as config.yml
+-- Dockerfile
+-- Makefile
+-- requirements.txt
+-- requirements-dev.txt
+-- pyrightconfig.json
+-- chisel/
    +-- __init__.py
    +-- config.py         # ChiselConfig, RepoConfig, PollSourceConfig, load_config()
    +-- api.py            # ChiselManager: in-memory state, submit/dedup logic
    +-- worker.py         # worker_loop(), run_job(), poll helpers, subprocess management
```

---

## Configuration

### Secrets (environment variables)

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Discord bot token (bot disabled if absent) |
| `ANTHROPIC_API_KEY` | API key auth for the `claude` subprocess (mutually exclusive with `CLAUDE_CODE_OAUTH_TOKEN`) |
| `CLAUDE_CODE_OAUTH_TOKEN` | OAuth token auth for the `claude` subprocess (subscription-based; mutually exclusive with `ANTHROPIC_API_KEY`) |
| `GITHUB_TOKEN` | Used by `gh` CLI for branch push and PR creation |

### `config.yml`

Loaded from `/config/config.yml` by default. Override with `CONFIG_PATH` env var.
See `config.yml.example` for all options with documentation.

Key fields:

```yaml
git_user_name: Chisel Bot
git_user_email: chisel@example.com
agent_context_path: /config/agent_context.md
repos_base_path: /repos
log_dir: /logs
max_turns: 40        # agent turn budget per job
job_timeout: 0       # wall-clock seconds; 0 = indefinite
poll_interval_seconds: 10  # sleep between poll cycles when no work is found

poll_sources:
  - name: Exception Tracker        # shown in ops channel
    url: https://example.com/chisel/poll
    basic_auth: "chisel:password"  # omit if not needed

discord:
  ops_channel_id: 0
  slash_command_prefix: ""
  allowed_roles: []  # Discord role IDs; empty = all users allowed

repos:
  - github_url: https://github.com/org/repo
    main_branch: main
    context: >
      Description of this repo shown to the agent in every prompt.
```

On startup, repos are cloned to `<repos_base_path>/<repo-name>` if not already present.
The `gh` CLI is authenticated using `GITHUB_TOKEN`.

---

## Polling

Chisel has no HTTP server. Instead it polls a configurable list of sources for work.

### Scheduling

Each cycle:
1. Check the Discord `/chisel` queue (priority). If a job is queued, run it.
2. Otherwise, POST to each poll source in order. Take the first job returned (HTTP 200).
3. If all sources return 204 (no work), sleep `poll_interval_seconds` and repeat.

After completing a job, Chisel retries the result callback with exponential backoff (initial
5s, doubling up to 120s max) until a 2xx response is received. The next job is not started
until the callback is successfully delivered.

### Poll Request

Chisel sends `POST <url>` with an empty JSON body `{}`. If `basic_auth` is configured,
an `Authorization: Basic <base64>` header is included.

### Poll Response

**204 No Content** — no work available.

**200 OK** — work available:
```json
{
  "message": "Fully rendered task description passed verbatim to the agent",
  "requester_id": "a1b2c3d4",
  "callback_url": "https://caller.example.com/some/callback/path"
}
```

A 200 response is a commitment: the same job must not be returned by a subsequent poll.

### Callback

POSTed to `callback_url` on job completion:

```json
{
  "job_id": "550e8400-...",
  "requester_id": "a1b2c3d4",
  "status": "success",
  "message": "PR created: fix: prevent NPE in FooManager",
  "summary": "Concise narrative of what was done or why not (<=300 words)",
  "detail": "Step-by-step execution log with every file examined and decision made",
  "pr_url": "https://github.com/org/repo/pull/42"
}
```

- `status`: one of `success`, `failure`, `declined`
- `pr_url`: only present on `success`
- `summary` and `detail` are always present (may be empty if the agent crashed before
  writing them)
- User aborts via `/abort` produce `status: "failure"` with `message: "Aborted by <name>"`

---

## Discord Bot

Enabled when `DISCORD_TOKEN` is set. All command responses are ephemeral.

| Command | Description |
|---|---|
| `/{p}chisel <request>` | Submit a job; DMs the user on completion |
| `/{p}abort` | Terminate the currently running job |
| `/{p}jobs` | List queued and running jobs |

`{p}` is the optional `slash_command_prefix` from config.

Job status is posted to the configured `ops_channel_id` for all jobs regardless of source
(Discord or poll):

```
[STARTED]  `{job_id[:8]}` | req: `{requester_id}` | source: {name}
[SUCCESS]  `{job_id[:8]}` | {pr_url} | {short_message}
[FAILURE]  `{job_id[:8]}` | {short_message}
[DECLINED] `{job_id[:8]}` | {short_message}
```

`source` is the Discord display name for `/chisel` jobs, or the poll source `name` for
polled jobs.

On all outcomes, the following non-empty files are attached: `prompt` (user-supplied),
`summary`, `detail`, and `abort` (if present). Empty files are omitted.

---

## Agent Execution

Each job:

1. Checks for an existing remote branch matching `chisel/<requester_id>-*`; declines if
   found (avoids duplicate PRs for the same request).
2. Creates a fresh branch `chisel/<requester_id>-<timestamp>` in each configured repo.
3. Builds a prompt from `agent_context.md` + repo context blocks + the submitted message.
4. Invokes `claude` with `--dangerously-skip-permissions`, `--output-format stream-json`,
   `--disallowedTools WebFetch,WebSearch`, and `--max-turns <n>`.
5. Streams stdout to both the job log file and the orchestrator's stdout (for live container
   log visibility).
6. On clean exit with `CHISEL_COMMIT_MSG.txt` present: commits, pushes, opens a PR.
7. On abort file or error result: sends `declined` or `failure` callback, no git operations.

Agent environment: `DISCORD_TOKEN` and `GITHUB_TOKEN` are stripped. The agent cannot push
branches or post to Discord.

Per-job logs are written to `<log_dir>/<timestamp>-<job_id>/`:
- `CHISEL_PROMPT.txt` - user-supplied portion of the prompt
- `CHISEL_FULL_PROMPT.txt` - full prompt sent to the agent (preamble + repo context + user prompt)
- `agent.log` - combined agent stdout and stderr (stderr lines prefixed with `[stderr]`)
- `workspace/` - agent working directory containing output files

---

## Development

```
make venv       # create .venv and install dependencies
make lint       # run pylint
make typecheck  # run pyright (strict)
make test       # lint + typecheck
```
