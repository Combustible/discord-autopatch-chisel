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
    source_user_id: Optional[int]  # Discord user ID; None for poll-sourced jobs
    source_label: str              # display name for ops channel


@dataclass
class JobResult:
    job_id: str
    requester_id: str
    status: str          # "success" | "failure" | "declined"
    message: str         # short human-readable status (<=200 chars)
    summary: str         # contents of CHISEL_SUMMARY.txt
    detail: str          # contents of CHISEL_DETAIL.txt
    abort: str           # contents of CHISEL_ABORT.txt (empty if not aborted)
    pr_url: Optional[str]


class ChiselManager:
    def __init__(self) -> None:
        self.current_job: Optional[PendingJob] = None
        self.pending: list[PendingJob] = []
        self._queue: asyncio.Queue[PendingJob] = asyncio.Queue()
        self.current_proc: Optional[asyncio.subprocess.Process] = None  # pylint: disable=no-member
        self.abort_event: asyncio.Event = asyncio.Event()
        self.aborting_user: Optional[str] = None

    def submit(
        self,
        requester_id: str,
        message: str,
        callback_fn: Callable[[JobResult], Awaitable[None]],
        source_label: str,
        source_user_id: Optional[int] = None,
    ) -> tuple[str, str]:
        """Submit a Discord job. Returns (job_id, status) where status is 'queued' or 'duplicate'."""
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
            source_label=source_label,
        )
        self.pending.append(job)
        self._queue.put_nowait(job)
        return job.job_id, "queued"

    def try_get_discord_job(self) -> Optional[PendingJob]:
        """Non-blocking: return a queued Discord job if available, else None."""
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def abort(self, user_display_name: str) -> None:
        """Signal the current job to abort, recording who triggered it."""
        self.aborting_user = user_display_name
        self.abort_event.set()
        if self.current_proc is not None:
            self.current_proc.terminate()

    def list_pending(self) -> list[PendingJob]:
        """Return all pending and currently-running jobs, in submission order."""
        return list(self.pending)
