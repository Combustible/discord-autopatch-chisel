# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Byron Marohn
import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Optional


@dataclass
class PendingJob:
    job_id: str
    requester_id: str
    message: str
    callback_fn: Callable[["JobResult"], Awaitable[None]]
    submitted_at: float
    source_user_id: Optional[int]  # Discord user ID; None for HTTP /submit


@dataclass
class JobResult:
    job_id: str
    requester_id: str
    status: str          # "success" | "failure" | "declined"
    message: str         # short human-readable status (<=200 chars)
    summary: str         # contents of CHISEL_SUMMARY.txt
    detail: str          # contents of CHISEL_DETAIL.txt
    pr_url: Optional[str]


class ChiselManager:
    def __init__(self) -> None:
        self.current_job: Optional[PendingJob] = None
        self.pending: list[PendingJob] = []
        self._queue: asyncio.Queue[PendingJob] = asyncio.Queue()
        self.current_proc: Optional[asyncio.subprocess.Process] = None  # pylint: disable=no-member
        self.abort_event: asyncio.Event = asyncio.Event()

    def submit(
        self,
        requester_id: str,
        message: str,
        callback_fn: Callable[[JobResult], Awaitable[None]],
        source_user_id: Optional[int] = None,
    ) -> tuple[str, str]:
        """Submit a job. Returns (job_id, status) where status is 'queued' or 'duplicate'."""
        if self.current_job and self.current_job.requester_id == requester_id:
            return self.current_job.job_id, "duplicate"
        for job in self.pending:
            if job.requester_id == requester_id:
                return job.job_id, "duplicate"

        job = PendingJob(
            job_id=str(uuid.uuid4()),
            requester_id=requester_id,
            message=message,
            callback_fn=callback_fn,
            submitted_at=time.time(),
            source_user_id=source_user_id,
        )
        self.pending.append(job)
        self._queue.put_nowait(job)
        return job.job_id, "queued"

    def list_pending(self) -> list[PendingJob]:
        """Return all pending and currently-running jobs, in submission order."""
        return list(self.pending)

    async def get_next_job(self) -> PendingJob:
        """Block until a job is available and return it."""
        return await self._queue.get()
