"""Direct-script compatibility for workflows.runtime.wecom_bot."""
if __package__:
    from ._compat import forward
else:
    from _compat import forward

forward("jobs.wecom_bot", __name__)
