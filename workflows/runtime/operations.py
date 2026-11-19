"""Application operations injected into HTTP handlers at startup."""

from workflows.market.daily_scheduler import get_scheduler
from workflows.market.digest_scheduler import get_digest_scheduler, get_midday_scheduler
from workflows.market.intraday_scheduler import get_intraday_scheduler


class BackendOperations:
    async def trigger_sync(self):
        return await get_scheduler().trigger_now()

    def scheduler_status(self):
        return {
            **get_scheduler().status(),
            "mail_digest": get_digest_scheduler().status(),
            "midday_digest": get_midday_scheduler().status(),
            "intraday_watch": get_intraday_scheduler().status(),
        }

    def sync_reports(self, group_id):
        from workflows.reports.sync import ZSXQ2PGSync

        ZSXQ2PGSync(group_id).run()
