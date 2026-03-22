# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
"""Worker loop and job execution for Chisel."""
import asyncio
import base64
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

import aiohttp

from .api import ChiselManager, JobResult, PendingJob
from .config import ChiselConfig, PollSourceConfig, RepoConfig

if TYPE_CHECKING:
    from bot import ChiselBot

logger = logging.getLogger(__name__)


def build_prompt(config: ChiselConfig, request: str) -> str:
    preamble = Path(config.agent_context_path).read_text(encoding='utf-8')
    repo_sections: list[str] = []
    for repo in config.repos:
        name = Path(repo.local_path).name
        repo_sections.append(
            f"## Repository: {name}\n"
            f"Path: `{repo.local_path}`\n"
            f"Main branch: `{repo.main_branch}`\n\n"
            f"{repo.context.strip()}"
        )
    repos_block = "\n\n".join(repo_sections)
    return f"{preamble}\n\n---\n\n{repos_block}\n\n---\n\n{request}"


def _repo_owner_name(github_url: str) -> str:
    """Extract 'owner/repo' from a GitHub HTTPS URL."""
    path = urlparse(github_url).path.strip('/')
    if path.endswith('.git'):
        path = path[:-4]
    return path


async def run_cmd(
    cmd: list[str],
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> tuple[str, str]:
    """Run a subprocess. Returns (stdout, stderr). Raises RuntimeError on non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"{cmd[0]} failed (exit {proc.returncode}): {err.decode(errors='replace')}"
        )
    return out.decode(errors='replace'), err.decode(errors='replace')


async def _retry(
    coro_fn: Callable[[], Awaitable[None]],
    label: str,
    max_attempts: Optional[int] = None,
) -> None:
    """Call coro_fn(), retrying with exponential backoff on any exception.

    If max_attempts is None, retries indefinitely. If max_attempts is set,
    gives up after that many failures and logs a final error.
    """
    delay = 5.0
    attempt = 0
    while True:
        attempt += 1
        try:
            await coro_fn()
            return
        except Exception:  # pylint: disable=broad-exception-caught
            if max_attempts is not None and attempt >= max_attempts:
                logger.exception("%s: failed after %d attempts, giving up", label, attempt)
                return
            logger.exception(
                "%s: attempt %d failed, retrying in %.0fs", label, attempt, delay
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120.0)


async def _post_ops_start(
    bot: Optional["ChiselBot"], config: ChiselConfig, job: PendingJob
) -> None:
    msg = (
        f"[STARTED] `{job.job_id[:8]}` | "
        f"req: `{job.requester_id}` | "
        f"source: {job.source_label}"
    )
    if bot is not None and config.discord.ops_channel_id:
        await bot.post_ops(msg)
    else:
        logger.info(msg)


async def _post_ops_complete(
    bot: Optional["ChiselBot"], config: ChiselConfig, result: JobResult,
    user_prompt: str,
) -> None:
    if result.status == "success":
        header = f"[SUCCESS] `{result.job_id[:8]}` | {result.pr_url} | {result.message}"
    elif result.status == "failure":
        header = f"[FAILURE] `{result.job_id[:8]}` | {result.message}"
    else:
        header = f"[DECLINED] `{result.job_id[:8]}` | {result.message}"

    short_id = result.job_id[:8]
    attach_files: list[tuple[str, str]] = []
    if user_prompt:
        attach_files.append((f"prompt-{short_id}.txt", user_prompt))
    if result.summary:
        attach_files.append((f"summary-{short_id}.txt", result.summary))
    if result.detail:
        attach_files.append((f"detail-{short_id}.txt", result.detail))
    if result.abort:
        attach_files.append((f"abort-{short_id}.txt", result.abort))

    if bot is not None and config.discord.ops_channel_id:
        await bot.post_ops(header, files=attach_files)
    else:
        logger.info(header)
        if result.summary:
            logger.info("Summary:\n%s", result.summary)


async def _try_poll_source(
    source: PollSourceConfig,
    session: aiohttp.ClientSession,
) -> Optional[PendingJob]:
    """Poll a single source. Returns a PendingJob on 200, None on 204 or unexpected status."""
    headers: dict[str, str] = {}
    if source.basic_auth:
        encoded = base64.b64encode(source.basic_auth.encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"

    async with session.post(
        source.url, headers=headers, json={},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status == 204:
            return None
        if resp.status != 200:
            logger.warning(
                "Poll source '%s' returned unexpected status %d", source.name, resp.status
            )
            return None

        data: dict[str, object] = await resp.json()
        message = str(data["message"])
        requester_id = str(data["requester_id"])
        callback_url = str(data["callback_url"])

    async def _http_callback(result: JobResult) -> None:
        payload: dict[str, object] = {
            "job_id": result.job_id,
            "requester_id": result.requester_id,
            "status": result.status,
            "message": result.message,
            "summary": result.summary,
            "detail": result.detail,
        }
        if result.pr_url is not None:
            payload["pr_url"] = result.pr_url
        async with session.post(
            callback_url, json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status >= 400:
                raise RuntimeError(
                    f"Callback to {callback_url} returned HTTP {r.status}"
                )

    return PendingJob(
        job_id=str(uuid.uuid4()),
        requester_id=requester_id,
        message=message,
        callback_fn=_http_callback,
        submitted_at=time.time(),
        source_user_id=None,
        source_label=source.name,
    )


async def _poll_sources(
    sources: list[PollSourceConfig],
    session: aiohttp.ClientSession,
) -> Optional[PendingJob]:
    """Try each source in order; return the first job found, or None if all are empty."""
    for source in sources:
        try:
            job = await _try_poll_source(source, session)
            if job is not None:
                logger.info("Claimed job from poll source '%s': %s", source.name, job.requester_id)
                return job
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("Error polling source '%s'", source.name)
    return None


async def run_job(
    job: PendingJob, manager: ChiselManager, config: ChiselConfig
) -> JobResult:
    """Execute a single job end-to-end. Returns a JobResult on any outcome."""
    timestamp = int(time.time())
    branch_name = f"chisel/{job.requester_id}-{timestamp}"

    # --- Step 1: Remote branch dedup ---
    for repo in config.repos:
        stdout, _ = await run_cmd([
            "git", "-C", repo.local_path, "ls-remote", "origin",
            f"refs/heads/chisel/{job.requester_id}-*",
        ])
        if stdout.strip():
            return JobResult(
                job_id=job.job_id,
                requester_id=job.requester_id,
                status="declined",
                message="Open branch already exists for this request",
                summary="",
                detail="",
                abort="",
                pr_url=None,
            )

    # --- Step 2: Prep repos ---
    for repo in config.repos:
        await run_cmd(["git", "-C", repo.local_path, "fetch", "origin"])
        await run_cmd([
            "git", "-C", repo.local_path, "checkout", "-b",
            branch_name, f"origin/{repo.main_branch}",
        ])
        await run_cmd(["git", "-C", repo.local_path, "clean", "-fd"])

    # --- Step 3: Create workspace dir ---
    job_dir = Path(config.log_dir) / f"{timestamp}-{job.job_id}"
    workspace_dir = job_dir / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # --- Step 4: Build and write prompt ---
    prompt = build_prompt(config, job.message)
    (job_dir / "CHISEL_PROMPT.txt").write_text(job.message, encoding='utf-8')
    (job_dir / "CHISEL_FULL_PROMPT.txt").write_text(prompt, encoding='utf-8')

    # --- Steps 5-7: Launch subprocess and stream ---
    result_event: dict[str, object] = {}
    killed_reason: Optional[str] = None

    log_path = job_dir / "agent.log"
    with open(log_path, 'w', encoding='utf-8') as log_file:
        env: dict[str, str] = {
            k: v for k, v in os.environ.items()
            if k not in ("DISCORD_TOKEN", "GITHUB_TOKEN")
        }
        env["DISABLE_AUTOUPDATER"] = "1"

        cmd = [
            "claude", "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            "--dangerously-skip-permissions",
            "--disallowedTools", "WebFetch,WebSearch",
            "--max-turns", str(config.max_turns),
        ]

        logger.info("Launching agent for job %s: %s", job.job_id, " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workspace_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        manager.current_proc = proc

        proc_stdout = proc.stdout
        proc_stderr = proc.stderr
        assert proc_stdout is not None
        assert proc_stderr is not None

        async def _read_stdout() -> None:
            while True:
                line = await proc_stdout.readline()
                if not line:
                    break
                text = line.decode('utf-8', errors='replace').rstrip('\n')
                log_file.write(text + '\n')
                log_file.flush()
                sys.stdout.write(text + '\n')
                sys.stdout.flush()
                try:
                    obj: dict[str, object] = json.loads(text)
                    if obj.get('type') == 'result':
                        result_event.update(obj)
                except json.JSONDecodeError:
                    pass

        async def _read_stderr() -> None:
            while True:
                line = await proc_stderr.readline()
                if not line:
                    break
                text = line.decode('utf-8', errors='replace').rstrip('\n')
                log_file.write('[stderr] ' + text + '\n')
                log_file.flush()
                sys.stderr.write('[agent stderr] ' + text + '\n')
                sys.stderr.flush()

        stdout_task: asyncio.Task[None] = asyncio.create_task(_read_stdout())
        stderr_task: asyncio.Task[None] = asyncio.create_task(_read_stderr())

        # Poll loop: drain stdout while checking for abort and timeout
        deadline: Optional[float] = None
        if config.job_timeout > 0:
            deadline = asyncio.get_running_loop().time() + config.job_timeout

        while not stdout_task.done():
            if manager.abort_event.is_set():
                proc.terminate()
                aborting_user = manager.aborting_user or "operator"
                killed_reason = f"Aborted by {aborting_user}"
                break

            now = asyncio.get_running_loop().time()
            if deadline is not None and now >= deadline:
                proc.terminate()
                killed_reason = "Job killed: timeout"
                break

            wait_secs = 2.0
            if deadline is not None:
                wait_secs = min(wait_secs, deadline - asyncio.get_running_loop().time())

            done, _ = await asyncio.wait([stdout_task], timeout=max(0.1, wait_secs))
            if done:
                break

        await proc.wait()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        logger.info("Agent exited with returncode %d for job %s", proc.returncode, job.job_id)
        if result_event:
            logger.info("Agent result event for job %s: %s", job.job_id, result_event)
        else:
            logger.warning("No result event captured from agent for job %s", job.job_id)
        manager.current_proc = None


    # --- Step 8: Read output files ---
    def _read_file(name: str) -> str:
        p = workspace_dir / name
        if p.exists():
            return p.read_text(encoding='utf-8', errors='replace')
        return ""

    abort_text = _read_file("CHISEL_ABORT.txt")
    summary = _read_file("CHISEL_SUMMARY.txt")
    detail = _read_file("CHISEL_DETAIL.txt")
    commit_msg = _read_file("CHISEL_COMMIT_MSG.txt").strip()

    # --- Step 9: Determine outcome ---
    if killed_reason:
        return JobResult(
            job_id=job.job_id,
            requester_id=job.requester_id,
            status="failure",
            message=killed_reason[:200],
            summary=summary,
            detail=detail,
            abort=abort_text,
            pr_url=None,
        )

    if abort_text.strip():
        first_line = abort_text.split('\n')[0].strip()[:200]
        return JobResult(
            job_id=job.job_id,
            requester_id=job.requester_id,
            status="declined",
            message=first_line or "Agent aborted",
            summary=summary,
            detail=detail,
            abort=abort_text,
            pr_url=None,
        )

    if result_event.get('is_error'):
        subtype = str(result_event.get('subtype', 'unknown'))
        return JobResult(
            job_id=job.job_id,
            requester_id=job.requester_id,
            status="failure",
            message=f"Agent error: {subtype}"[:200],
            summary=summary,
            detail=detail,
            abort=abort_text,
            pr_url=None,
        )

    if not commit_msg:
        return JobResult(
            job_id=job.job_id,
            requester_id=job.requester_id,
            status="declined",
            message="Agent found no changes to make",
            summary=summary,
            detail=detail,
            abort=abort_text,
            pr_url=None,
        )

    # --- Step 10: Git operations ---
    repos_with_changes: list[RepoConfig] = []
    for repo in config.repos:
        status_out, _ = await run_cmd([
            "git", "-C", repo.local_path, "status", "--porcelain",
        ])
        if status_out.strip():
            repos_with_changes.append(repo)

    if len(repos_with_changes) > 1:
        return JobResult(
            job_id=job.job_id,
            requester_id=job.requester_id,
            status="failure",
            message="Agent modified multiple repositories; aborting git operations",
            summary=summary,
            detail=detail,
            abort=abort_text,
            pr_url=None,
        )

    if not repos_with_changes:
        return JobResult(
            job_id=job.job_id,
            requester_id=job.requester_id,
            status="declined",
            message="Agent found no changes to make",
            summary=summary,
            detail=detail,
            abort=abort_text,
            pr_url=None,
        )

    changed_repo = repos_with_changes[0]
    await run_cmd(["git", "-C", changed_repo.local_path, "add", "-A"])
    await run_cmd([
        "git", "-C", changed_repo.local_path,
        "-c", f"user.name={config.git_user_name}",
        "-c", f"user.email={config.git_user_email}",
        "commit", "-m", commit_msg,
    ])
    await run_cmd([
        "git", "-C", changed_repo.local_path, "push", "origin", branch_name,
    ])

    owner_repo = _repo_owner_name(changed_repo.github_url)
    title = commit_msg.split('\n')[0][:72]
    pr_out, _ = await run_cmd([
        "gh", "pr", "create",
        "--repo", owner_repo,
        "--head", branch_name,
        "--title", title,
        "--body", summary,
    ])
    pr_url = pr_out.strip()

    return JobResult(
        job_id=job.job_id,
        requester_id=job.requester_id,
        status="success",
        message=f"PR created: {title}"[:200],
        summary=summary,
        detail=detail,
        abort=abort_text,
        pr_url=pr_url,
    )


async def worker_loop(
    manager: ChiselManager,
    config: ChiselConfig,
    bot: Optional["ChiselBot"],
    session: aiohttp.ClientSession,
) -> None:
    """Main scheduling loop: prefer Discord queue jobs, then poll sources, then sleep."""
    while True:
        # Check Discord queue first (priority)
        job = manager.try_get_discord_job()

        # If no Discord job, poll external sources in order
        if job is None and config.poll_sources:
            job = await _poll_sources(config.poll_sources, session)

        # No work found; sleep and try again
        if job is None:
            await asyncio.sleep(config.poll_interval_seconds)
            continue

        # Rebind as non-optional for use in lambdas (pyright can't narrow through closures)
        current_job: PendingJob = job

        # Add to pending for /jobs visibility (Discord jobs already added by submit())
        if current_job not in manager.pending:
            manager.pending.append(current_job)

        manager.current_job = current_job
        manager.abort_event.clear()
        manager.aborting_user = None

        result: JobResult
        try:
            await _retry(
                lambda: _post_ops_start(bot, config, current_job),
                f"ops start {current_job.job_id[:8]}",
                max_attempts=5,
            )
            result = await run_job(current_job, manager, config)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("Unhandled error in run_job for %s", current_job.job_id)
            result = JobResult(
                job_id=current_job.job_id,
                requester_id=current_job.requester_id,
                status="failure",
                message="Internal orchestrator error",
                summary="",
                detail="",
                abort="",
                pr_url=None,
            )
        finally:
            manager.current_job = None
            manager.current_proc = None
            if current_job in manager.pending:
                manager.pending.remove(current_job)

        await _retry(
            lambda: _post_ops_complete(bot, config, result, current_job.message),
            f"ops complete {current_job.job_id[:8]}",
            max_attempts=5,
        )
        await _retry(
            lambda: current_job.callback_fn(result),
            f"callback {current_job.job_id[:8]}",
        )
