"""Direct-script compatibility for workflows.reports.sync."""
if __package__:
    from ._compat import forward
else:
    from _compat import forward

forward("jobs.sync_zxsq_to_pg", __name__)
