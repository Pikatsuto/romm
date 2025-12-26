from handler.retroarch_handler import retroarch_handler
from logger.logger import log
from tasks.tasks import PeriodicTask, TaskType


class CleanupRetroArchTask(PeriodicTask):
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
        if not self.enabled:
            self.unschedule()
            return

        cleaned_count = await retroarch_handler.cleanup_stale_sessions()
        if cleaned_count > 0:
            log.info(f"Cleaned up {cleaned_count} stale RetroArch sessions")


cleanup_retroarch_task = CleanupRetroArchTask()