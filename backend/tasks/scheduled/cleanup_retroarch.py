"""Scheduled task for cleaning up inactive RetroArch sessions.

This module provides a periodic task that runs every 5 minutes to detect
and clean up RetroArch streaming sessions that have been inactive for too long,
freeing up resources and session slots.
"""

from handler.retroarch_handler import retroarch_handler
from logger.logger import log
from tasks.tasks import PeriodicTask, TaskType


class CleanupRetroArchTask(PeriodicTask):
    """Periodic task for cleaning up stale RetroArch sessions.

    This task runs every 5 minutes and removes sessions that have exceeded
    the inactivity timeout defined in the retroarch_handler module.

    Attributes:
        title: Display name for the task.
        description: Human-readable description of what the task does.
        task_type: Category of the task (CLEANUP).
        cron_string: Cron expression defining execution schedule.
    """

    def __init__(self):
        super().__init__(
            title="Scheduled RetroArch cleanup",
            description="Cleans up inactive RetroArch sessions",
            task_type=TaskType.CLEANUP,
            enabled=True,
            manual_run=False,
            cron_string="*/5 * * * *",  # Every 5 minutes
            func="tasks.scheduled.cleanup_retroarch.cleanup_retroarch_task.run",
        )

    async def run(self) -> None:
        """Execute the cleanup task.

        Checks if the task is enabled before running. If disabled, unschedules
        itself to prevent future executions. Otherwise, delegates to the
        retroarch_handler to clean up stale sessions.
        """
        if not self.enabled:
            self.unschedule()
            return

        cleaned_count = await retroarch_handler.cleanup_stale_sessions()
        if cleaned_count > 0:
            log.info(f"Cleaned up {cleaned_count} stale RetroArch sessions")


cleanup_retroarch_task = CleanupRetroArchTask()