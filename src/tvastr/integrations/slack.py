"""Slack integration — satisfies the :class:`~tvastr.agent.context.Notifier` protocol."""

from __future__ import annotations

import httpx

from tvastr.config import Settings
from tvastr.logging import get_logger

log = get_logger(__name__)


class MockSlackNotifier:
    """Captures notifications in memory and logs them instead of posting."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def notify(self, message: str) -> bool:
        self.sent.append(message)
        log.info("slack.notify", message=message, mocked=True)
        return True


class SlackNotifier:
    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    def notify(self, message: str) -> bool:
        try:
            resp = httpx.post(self.webhook_url, json={"text": message}, timeout=10.0)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("slack.notify_failed", error=str(exc))
            return False
        log.info("slack.notify")
        return True


def build_notifier(settings: Settings) -> MockSlackNotifier | SlackNotifier:
    if settings.use_mocks or not settings.slack_webhook_url:
        return MockSlackNotifier()
    return SlackNotifier(settings.slack_webhook_url)
