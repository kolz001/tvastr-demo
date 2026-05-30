"""Tools the agent can invoke. Each is a thin, testable function over the
:class:`~tvastr.agent.context.AgentContext` — the building blocks the graph nodes
compose into a remediation run.
"""

from tvastr.agent.tools.code_retrieval import extract_stack_files, retrieve_code
from tvastr.agent.tools.fix_generation import generate_fix
from tvastr.agent.tools.github_search import search_codebase
from tvastr.agent.tools.pr_creation import open_pull_request
from tvastr.agent.tools.slack_notify import send_notification

__all__ = [
    "extract_stack_files",
    "generate_fix",
    "open_pull_request",
    "retrieve_code",
    "search_codebase",
    "send_notification",
]
