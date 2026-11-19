"""Direct-script compatibility for workflows.diagnostics.agent_trace."""
if __package__:
    from ._compat import forward
else:
    from _compat import forward

forward("jobs.agent_trace_demo", __name__)
