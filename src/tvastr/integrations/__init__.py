"""External integrations: GitHub (code + PRs) and Slack (notifications).

Each integration ships a real client and a mock. ``build_*`` factories pick based
on settings so the rest of the system depends only on the behavior, not the wiring.
"""

from tvastr.integrations.github import GitHubClient, MockGitHubClient, build_code_host
from tvastr.integrations.slack import MockSlackNotifier, SlackNotifier, build_notifier

__all__ = [
    "GitHubClient",
    "MockGitHubClient",
    "MockSlackNotifier",
    "SlackNotifier",
    "build_code_host",
    "build_notifier",
]
