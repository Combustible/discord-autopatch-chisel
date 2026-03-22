# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml


@dataclass
class RepoConfig:
    github_url: str
    main_branch: str
    context: str
    local_path: str  # derived: repos_base_path / basename(github_url)


@dataclass
class PollSourceConfig:
    name: str        # human-readable label shown in ops channel
    url: str
    basic_auth: str | None  # "user:password"; sent as Authorization: Basic <base64>


@dataclass
class DiscordConfig:
    ops_channel_id: int
    slash_command_prefix: str
    allowed_role_ids: set[int]


@dataclass
class ChiselConfig:
    repos: list[RepoConfig]
    repos_base_path: str
    log_dir: str
    agent_context_path: str
    max_turns: int           # default 40
    job_timeout: int         # 0 = indefinite
    git_user_name: str       # used for commits made by the orchestrator
    git_user_email: str      # used for commits made by the orchestrator
    poll_sources: list[PollSourceConfig]
    poll_interval_seconds: int  # sleep between poll cycles when no work is found
    discord: DiscordConfig   # populated even if bot disabled; ops_channel_id=0 is sentinel


def load_config(path: str) -> ChiselConfig:
    """Load and validate config from a YAML file. Raises ValueError on invalid config."""
    with open(path, encoding='utf-8') as f:
        data: dict[str, Any] = yaml.safe_load(f) or {}

    repos_base_path: str = str(data.get('repos_base_path', '/repos'))

    raw_repos = cast(list[Any], data.get('repos') or [])
    repos: list[RepoConfig] = []
    for i, r in enumerate(raw_repos):
        r_dict = cast(dict[str, Any], r if isinstance(r, dict) else {})
        github_url = r_dict.get('github_url')
        if not github_url:
            raise ValueError(f"repos[{i}]: github_url is required")
        local_path = os.path.join(repos_base_path, Path(str(github_url)).name)
        repos.append(RepoConfig(
            github_url=str(github_url),
            main_branch=str(r_dict.get('main_branch', 'main')),
            context=str(r_dict.get('context', '')),
            local_path=local_path,
        ))

    raw_sources = cast(list[Any], data.get('poll_sources') or [])
    poll_sources: list[PollSourceConfig] = []
    for i, s in enumerate(raw_sources):
        s_dict = cast(dict[str, Any], s if isinstance(s, dict) else {})
        source_name = s_dict.get('name')
        source_url = s_dict.get('url')
        if not source_name:
            raise ValueError(f"poll_sources[{i}]: name is required")
        if not source_url:
            raise ValueError(f"poll_sources[{i}]: url is required")
        poll_sources.append(PollSourceConfig(
            name=str(source_name),
            url=str(source_url),
            basic_auth=str(s_dict['basic_auth']) if s_dict.get('basic_auth') else None,
        ))

    discord_data = cast(dict[str, Any], data.get('discord') or {})
    raw_roles = cast(list[Any], discord_data.get('allowed_roles') or [])
    discord = DiscordConfig(
        ops_channel_id=int(discord_data.get('ops_channel_id', 0)),
        slash_command_prefix=str(discord_data.get('slash_command_prefix', '')),
        allowed_role_ids=set(int(role) for role in raw_roles),
    )

    git_user_name = data.get('git_user_name')
    if not git_user_name:
        raise ValueError("config: git_user_name is required")

    git_user_email = data.get('git_user_email')
    if not git_user_email:
        raise ValueError("config: git_user_email is required")

    return ChiselConfig(
        repos=repos,
        repos_base_path=repos_base_path,
        log_dir=str(data.get('log_dir', '/logs')),
        agent_context_path=str(data.get('agent_context_path', '/config/agent_context.md')),
        max_turns=int(data.get('max_turns', 40)),
        job_timeout=int(data.get('job_timeout', 0)),
        git_user_name=str(git_user_name),
        git_user_email=str(git_user_email),
        poll_sources=poll_sources,
        poll_interval_seconds=int(data.get('poll_interval_seconds', 10)),
        discord=discord,
    )
