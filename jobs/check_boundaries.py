"""Direct-script compatibility for workflows.diagnostics.check_boundaries."""
if not __package__:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobs._compat import forward

forward("jobs.check_boundaries", __name__)
