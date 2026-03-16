# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
import asyncio
import logging
import os
import signal
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlparse

import aiohttp
from pydantic import BaseModel, ValidationError
from quart import Quart, jsonify, request

from chisel.api import ChiselManager, JobResult
from chisel.config import ChiselConfig, load_config
from chisel.worker import worker_loop

if TYPE_CHECKING:
    from bot import ChiselBot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic request model
# ---------------------------------------------------------------------------

class SubmitRequest(BaseModel):
    message: str
    requester_id: str
    callback_url: Optional[str] = None


# ---------------------------------------------------------------------------
# HTTP callback
# ---------------------------------------------------------------------------

async def http_callback(callback_url: str, result: JobResult) -> None:
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
    async with aiohttp.ClientSession() as session:
        try:
            await session.post(
                callback_url, json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            )
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("Callback POST to %s failed (not retrying)", callback_url)


# ---------------------------------------------------------------------------
# Quart app
# ---------------------------------------------------------------------------

def create_app(
    manager: ChiselManager,
    config: ChiselConfig,
    bot: Optional["ChiselBot"] = None,
) -> Quart:
    app = Quart(__name__)

    @app.post('/submit')
    async def submit_endpoint() -> Any:
        raw = await request.get_json(force=True)
        if raw is None:
            return jsonify({'error': 'expected JSON body'}), 400
        try:
            req = SubmitRequest.model_validate(raw)
        except ValidationError as e:
            return jsonify({'error': e.errors()}), 400

        cb_url = req.callback_url

        async def _callback(result: JobResult) -> None:
            if cb_url:
                await http_callback(cb_url, result)

        job_id, status = manager.submit(
            requester_id=req.requester_id,
            message=req.message,
            callback_fn=_callback,
            source_user_id=None,
        )
        return jsonify({'job_id': job_id, 'status': status})

    @app.get('/health')
    async def health() -> Any:
        return '', 200

    @app.before_serving
    async def startup() -> None:
        asyncio.create_task(worker_loop(manager, config, bot))

    return app


# ---------------------------------------------------------------------------
# Event loop helpers
# ---------------------------------------------------------------------------

def _mask_token(token: str) -> str:
    if len(token) <= 2:
        return '*' * len(token)
    return token[0] + '*' * (len(token) - 2) + token[-1]


async def _run_cmd_startup(cmd: list[str]) -> None:
    """Run a subprocess during startup. Raises RuntimeError on non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"{cmd[0]} failed (exit {proc.returncode}): {err.decode(errors='replace')}"
        )
    if out.strip():
        logger.info("%s output: %s", cmd[0], out.decode(errors='replace').strip())


async def _run_until_stopped(
    app: Quart,
    port: int,
    bot: Optional["ChiselBot"],
    discord_token: Optional[str],
    stop: asyncio.Event,
) -> None:
    """Run Quart and the optional Discord bot until a stop signal is received."""
    tasks: list[asyncio.Task[Any]] = [
        asyncio.create_task(app.run_task(host='0.0.0.0', port=port), name='quart'),
    ]
    if bot is not None and discord_token:
        tasks.append(asyncio.create_task(bot.start(discord_token), name='discord'))

    stop_task: asyncio.Task[Any] = asyncio.create_task(stop.wait(), name='stop')
    done, _ = await asyncio.wait([stop_task, *tasks], return_when=asyncio.FIRST_COMPLETED)
    stop_task.cancel()

    if stop_task in done:
        logger.info('Shutdown signal received, stopping...')
        for task in tasks:
            task.cancel()
        if bot is not None:
            await bot.close()
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    config_path = os.environ.get('CONFIG_PATH', '/config/config.yml')
    config = load_config(config_path)

    discord_token = os.environ.get('DISCORD_TOKEN')
    github_token = os.environ.get('GITHUB_TOKEN', '')
    anthropic_key = os.environ.get('ANTHROPIC_API_KEY', '')
    oauth_token = os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', '')

    logger.info(
        "Starting with config:\n"
        "  CONFIG_PATH=%s\n"
        "  port=%s\n"
        "  repos_base_path=%s\n"
        "  log_dir=%s\n"
        "  agent_context_path=%s\n"
        "  max_turns=%s\n"
        "  job_timeout=%s\n"
        "  git_user_name=%s\n"
        "  git_user_email=%s\n"
        "  DISCORD_TOKEN=%s\n"
        "  GITHUB_TOKEN=%s\n"
        "  ANTHROPIC_API_KEY=%s\n"
        "  CLAUDE_CODE_OAUTH_TOKEN=%s",
        config_path,
        config.port,
        config.repos_base_path,
        config.log_dir,
        config.agent_context_path,
        config.max_turns,
        config.job_timeout,
        config.git_user_name,
        config.git_user_email,
        _mask_token(discord_token) if discord_token else '(not set)',
        _mask_token(github_token) if github_token else '(not set)',
        _mask_token(anthropic_key) if anthropic_key else '(not set)',
        _mask_token(oauth_token) if oauth_token else '(not set)',
    )

    if anthropic_key and oauth_token:
        logger.warning(
            "Both ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN are set. "
            "The claude CLI will determine which is used; this is likely not what you want."
        )

    # --- Clone repos if not present ---
    for repo in config.repos:
        if not os.path.exists(repo.local_path):
            logger.info("Cloning %s -> %s", repo.github_url, repo.local_path)
            if github_token:
                parsed = urlparse(repo.github_url)
                clone_url = (
                    f"{parsed.scheme}://x-access-token:{github_token}"
                    f"@{parsed.netloc}{parsed.path}"
                )
            else:
                clone_url = repo.github_url
            await _run_cmd_startup(["git", "clone", clone_url, repo.local_path])
        else:
            logger.info("Repo already present at %s", repo.local_path)

    # --- Authenticate gh CLI ---
    # GITHUB_TOKEN in the environment is used automatically by gh; no login step needed.
    if github_token:
        await _run_cmd_startup(["gh", "auth", "setup-git"])
    else:
        logger.warning("GITHUB_TOKEN not set; gh CLI not authenticated")

    # --- Create log dir ---
    os.makedirs(config.log_dir, exist_ok=True)

    # --- Signal handlers ---
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    # --- Construct manager and optional bot ---
    manager = ChiselManager()
    bot: Optional["ChiselBot"] = None
    if discord_token:
        from bot import ChiselBot
        bot = ChiselBot(manager, config)

    app = create_app(manager, config, bot)

    try:
        await _run_until_stopped(app, config.port, bot, discord_token, stop)
    finally:
        logger.info('Shutdown complete.')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
