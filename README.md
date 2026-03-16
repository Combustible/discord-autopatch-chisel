# Chisel

Chisel is a Python microservice that accepts plain-text task prompts via HTTP or Discord,
invokes the Claude Code CLI agent against one or more locally-cloned git repositories, and
reports outcomes via HTTP callback or Discord DM.

It is designed for small, well-scoped, single-commit code changes. The agent reads the
codebase, makes a fix, and writes structured output files that the orchestrator uses to
commit, push, and open a pull request. If the task is out of scope or the agent cannot
identify a clean solution, it writes an abort file and the job is declined with no git
operations performed.

Chisel is generic: it knows nothing about exceptions, Discord channels, or fingerprints.
Any system that can POST a text prompt and receive a structured callback can use it. The
first client is a Minecraft server exception tracker that renders a template with exception
data and POSTs to `/submit`.

---

## Design Overview

- **No database.** All state is in-memory. The ops channel, log files, and git history are
  the durable record. State is lost on restart; deduplication is best-effort.
- **Sequential processing.** One job runs at a time. Additional submissions queue up.
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
+-- server.py              # Quart app factory, _run_until_stopped(), main()
+-- bot.py                 # ChiselBot (discord.py)
+-- agent_context.md       # Agent instructions (version-controlled, operator-editable)
+-- config.yml.example     # Documented config reference; copy/mount as config.yml
+-- Dockerfile
+-- Makefile
+-- requirements.txt
+-- requirements-dev.txt
+-- pyrightconfig.json
+-- chisel/
    +-- __init__.py
    +-- config.py          # ChiselConfig, RepoConfig, DiscordConfig, load_config()
    +-- api.py             # ChiselManager: in-memory state, submit/dedup logic
    +-- worker.py          # worker_loop(), run_job(), subprocess management
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
port: 8080
git_user_name: Chisel Bot
git_user_email: chisel@example.com
agent_context_path: /config/agent_context.md
repos_base_path: /repos
log_dir: /logs
max_turns: 40        # agent turn budget per job
job_timeout: 0       # wall-clock seconds; 0 = indefinite

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

## HTTP API

### `POST /submit`

Submit a job. Returns immediately.

**Request:**
```json
{
  "message": "Fix the exception below...",
  "requester_id": "a1b2c3d4",
  "callback_url": "http://caller/callback"
}
```

- `requester_id`: caller-defined string used for deduplication. Chisel generates `job_id`.
- `callback_url`: optional. If omitted, no callback is sent on completion.

**Response:**
```json
{ "job_id": "550e8400-...", "status": "queued" }
```
or
```json
{ "job_id": "550e8400-...", "status": "duplicate" }
```

`duplicate` means a job with the same `requester_id` is already queued or running.

### `GET /health`

Returns `200 OK`. No body.

---

## Callback Schema

POSTed to `callback_url` on job completion. Not sent for intermediate states.

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
(HTTP or Discord):

```
[STARTED]  `{job_id[:8]}` | req: `{requester_id}` | source: {/submit | <@user_id>}
[SUCCESS]  `{job_id[:8]}` | {pr_url} | {short_message}
[FAILURE]  `{job_id[:8]}` | {short_message}
[DECLINED] `{job_id[:8]}` | {short_message}
```

On `SUCCESS` or `FAILURE`, the ops post includes `summary` and `detail` as file attachments.

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

Per-job logs are written to `<log_dir>/<job_id>/`:
- `prompt.txt` - full prompt sent to the agent
- `agent.log` - full stream-json output
- `workspace/` - agent working directory containing output files

---

## Development

```
make venv       # create .venv and install dependencies
make lint       # run pylint
make typecheck  # run pyright (strict)
make test       # lint + typecheck
```
