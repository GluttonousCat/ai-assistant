"""Historical research programs and backwards-compatible workflow commands."""
import sys

from jobs._compat import WorkflowAliases

if WorkflowAliases not in sys.meta_path:
    sys.meta_path.append(WorkflowAliases)
