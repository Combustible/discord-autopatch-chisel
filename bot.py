# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
"""Discord bot for Chisel.

Exposes slash commands for submitting requests, aborting jobs, and listing
the queue. Posts job status updates to a configured ops channel.
"""
import hashlib
import io
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from chisel.api import ChiselManager, JobResult
from chisel.config import ChiselConfig

logger = logging.getLogger(__name__)


class ChiselBot(commands.Bot):
    """Discord bot that submits and monitors Chisel jobs."""

    def __init__(self, manager: ChiselManager, config: ChiselConfig) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.manager = manager
        self.config = config

    async def setup_hook(self) -> None:
        self._register_commands()
        await self.tree.sync()

    # --- Channel helpers ---

    async def _get_channel(self, channel_id: int) -> Optional[discord.TextChannel]:
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.NotFound:
                logger.exception("Discord channel %d not found", channel_id)
                return None
        return channel  # type: ignore[return-value]

    # --- Ops channel ---

    async def post_ops(
        self,
        message: str,
        files: Optional[list[tuple[str, str]]] = None,
    ) -> None:
        """Post a message to the ops channel with optional in-memory file attachments."""
        if not self.config.discord.ops_channel_id:
            return
        channel = await self._get_channel(self.config.discord.ops_channel_id)
        if channel is None:
            return
        discord_files: list[discord.File] = []
        if files:
            for filename, content in files:
                discord_files.append(
                    discord.File(io.BytesIO(content.encode('utf-8')), filename=filename)
                )
        try:
            await channel.send(message, files=discord_files)
        except discord.DiscordException:
            logger.exception("Failed to post to ops channel")

    # --- DM helpers ---

    async def _dm_completion(self, discord_user_id: int, result: JobResult) -> None:
        """DM the submitting user with the job outcome."""
        try:
            user = await self.fetch_user(discord_user_id)
            await user.send(
                f"Your chisel request `{result.job_id[:8]}` is complete.\n"
                f"**Status:** {result.status}\n"
                f"{result.message}"
            )
        except discord.Forbidden:
            logger.exception("Could not DM user %d (DMs may be disabled)", discord_user_id)
        except discord.DiscordException:
            logger.exception("Failed to DM completion to user %d", discord_user_id)

    # --- Permission check ---

    def _check_allowed(self, interaction: discord.Interaction) -> bool:
        if not self.config.discord.allowed_role_ids:
            return True
        if not isinstance(interaction.user, discord.Member):
            return False
        return any(
            role.id in self.config.discord.allowed_role_ids
            for role in interaction.user.roles
        )

    # --- Slash command registration ---

    def _register_commands(self) -> None:
        p = self.config.discord.slash_command_prefix

        @self.tree.command(
            name=f"{p}chisel",
            description="Submit a code modification request to Chisel",
        )
        @app_commands.describe(request="Description of the change to make")
        async def cmd_chisel(
            interaction: discord.Interaction, request: str
        ) -> None:
            await interaction.response.defer(ephemeral=True)
            if not self._check_allowed(interaction):
                await interaction.followup.send(
                    "You don't have permission to use this command.", ephemeral=True
                )
                return

            user_id = interaction.user.id
            requester_id = hashlib.sha256(request.encode()).hexdigest()[:8]

            async def _callback(result: JobResult) -> None:
                await self._dm_completion(user_id, result)

            job_id, status = self.manager.submit(
                requester_id=requester_id,
                message=request,
                callback_fn=_callback,
                source_user_id=user_id,
            )

            if status == "queued":
                await interaction.followup.send(
                    f"Request queued as job `{job_id[:8]}`.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "An identical request is already in the queue.", ephemeral=True
                )

        @self.tree.command(
            name=f"{p}abort",
            description="Abort the currently running Chisel job",
        )
        async def cmd_abort(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            if not self._check_allowed(interaction):
                await interaction.followup.send(
                    "You don't have permission to use this command.", ephemeral=True
                )
                return

            if self.manager.current_job is None:
                await interaction.followup.send(
                    "No job is currently running.", ephemeral=True
                )
                return

            job_id = self.manager.current_job.job_id
            self.manager.abort_event.set()
            if self.manager.current_proc is not None:
                self.manager.current_proc.terminate()
            await interaction.followup.send(
                f"Abort signal sent to job `{job_id[:8]}`.", ephemeral=True
            )

        @self.tree.command(
            name=f"{p}jobs",
            description="List pending and running Chisel jobs",
        )
        async def cmd_jobs(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            if not self._check_allowed(interaction):
                await interaction.followup.send(
                    "You don't have permission to use this command.", ephemeral=True
                )
                return

            jobs = self.manager.list_pending()
            if not jobs:
                await interaction.followup.send("No jobs pending.", ephemeral=True)
                return

            lines: list[str] = []
            for job in jobs:
                submitted = int(job.submitted_at)
                tag = " **[running]**" if job is self.manager.current_job else ""
                lines.append(
                    f"`{job.job_id[:8]}` req:`{job.requester_id}` "
                    f"submitted:<t:{submitted}:R>{tag}"
                )
            await interaction.followup.send("\n".join(lines), ephemeral=True)
