"""Tool: send a human-facing notification (Slack)."""

from __future__ import annotations

from tvastr.agent.context import AgentContext
from tvastr.logging import get_logger

log = get_logger(__name__)


def send_notification(ctx: AgentContext, message: str) -> bool:
    log.info("tool.slack_notify")
    return ctx.notifier.notify(message)
