"""Direct-script compatibility for workflows.reports.fetch."""
if __package__:
    from ._compat import forward
else:
    from _compat import forward

forward("jobs.daily_fetch_zxsq", __name__)
