"""Compatibility namespace for commands now organized under workflows."""
import sys

from jobs._compat import WorkflowAliases

if WorkflowAliases not in sys.meta_path:
    sys.meta_path.append(WorkflowAliases)
