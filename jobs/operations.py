"""Application operations injected into HTTP handlers at startup."""

from jobs.scheduler import get_scheduler, get_digest_scheduler, get_intraday_scheduler


class BackendOperations:
    async def trigger_sync(self):
        return await get_scheduler().trigger_now()

    def scheduler_status(self):
        return {
            **get_scheduler().status(),
            "mail_digest": get_digest_scheduler().status(),
            "intraday_watch": get_intraday_scheduler().status(),
        }

    def sync_reports(self, group_id):
        from jobs.sync_zxsq_to_pg import ZSXQ2PGSync

        ZSXQ2PGSync(group_id).run()
