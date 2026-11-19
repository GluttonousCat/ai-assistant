"""Direct-script compatibility for workflows.reports.deduplicate."""
if __package__:
    from ._compat import forward
else:
    from _compat import forward

forward("jobs.merge_report_duplicates", __name__)
