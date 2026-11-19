"""Direct-script compatibility for workflows.diagnostics.smoke_mcp_stdio."""
if __package__:
    from ._compat import forward
else:
    from _compat import forward

forward("jobs.smoke_mcp_stdio", __name__)
