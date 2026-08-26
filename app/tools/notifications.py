"""Tells a human that a decision is waiting for them.

The agent runs hourly while nobody is watching. Without this, a $148/month
ticket sits in a dashboard nobody has open, and the human-in-the-loop depends
on someone happening to walk past it. Approvals are the one part of this system
that cannot be autonomous, so they are the one part that has to go and find a
person.

Every channel is optional. An unconfigured channel is skipped, not an error,
and a channel that fails is logged and stepped over: the audit already
succeeded, and losing a notification must never lose the finding. Delivery runs
off the request path for the same reason — a slow webhook cannot be allowed to
hold up an audit.
"""

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.trace import APPROVAL, INFO, WARN, tracer

logger = logging.getLogger(__name__)

# Kept short: this is a courtesy message, not a step of the workflow.
_SEVERITY_EMOJI = {"HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🟡"}


def configured_channels() -> List[str]:
    """Which channels will actually be used, for the readiness report."""
    channels = []
    if settings.SLACK_WEBHOOK_URL:
        channels.append("slack")
    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
        channels.append("telegram")
    return channels


def _dashboard_link() -> str:
    return settings.DASHBOARD_URL.rstrip("/") if settings.DASHBOARD_URL else ""


def _shape_line(ticket: Dict[str, Any]) -> str:
    """The change that will run, when the headline does not already say it.

    A rendered headline usually ends in the shape already; repeating it reads
    as noise in a chat message, where every line costs attention.
    """
    from app.tools.rationale import describe_shape

    shape = ticket.get("target_shape") or {}
    if not shape.get("memory"):
        return ""
    described = describe_shape(shape)
    return "" if described in str(ticket.get("proposed_action", "")) else described


def _summarise(ticket: Dict[str, Any]) -> Tuple[str, str, str, str]:
    """(emoji, headline, resource line, shape) — shared by every channel."""
    severity = (ticket.get("severity") or "MEDIUM").upper()
    emoji = _SEVERITY_EMOJI.get(severity, "🟠")
    saving = float(ticket.get("estimated_roi") or 0.0)
    resource = ticket.get("resource_id", "unknown")
    headline = f"${saving:,.2f}/mo · {resource}"
    return emoji, headline, resource, _shape_line(ticket)


# ----------------------------------------------------------------------
# Channels
# ----------------------------------------------------------------------
def _slack_payload(ticket: Dict[str, Any]) -> Dict[str, Any]:
    emoji, headline, resource, shape = _summarise(ticket)
    link = _dashboard_link()

    lines = [f"*{ticket.get('proposed_action', 'Action awaiting approval')}*"]
    if shape:
        lines.append(f"Applies: `{shape}`")
    if ticket.get("detailed_reason"):
        lines.append(f"_{ticket['detailed_reason']}_")

    blocks: List[Dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{emoji} *Approval needed* — {headline}\n" + "\n".join(lines),
            },
        }
    ]
    if link:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open the deck"},
                        "url": link,
                    }
                ],
            }
        )
    # `text` is the notification preview and the fallback for clients that do
    # not render blocks; Slack warns when it is missing.
    return {"text": f"{emoji} Approval needed — {headline}", "blocks": blocks}


def _telegram_payload(ticket: Dict[str, Any]) -> Dict[str, Any]:
    emoji, headline, resource, shape = _summarise(ticket)
    link = _dashboard_link()

    lines = [
        f"{emoji} <b>Approval needed</b> — {_escape(headline)}",
        _escape(ticket.get("proposed_action", "Action awaiting approval")),
    ]
    if shape:
        lines.append(f"Applies: <code>{_escape(shape)}</code>")
    if ticket.get("detailed_reason"):
        lines.append(f"<i>{_escape(ticket['detailed_reason'])}</i>")
    if link:
        lines.append(f'<a href="{_escape(link)}">Open the deck</a>')

    return {
        "chat_id": settings.TELEGRAM_CHAT_ID,
        "text": "\n\n".join(lines),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }


def _escape(text: str) -> str:
    """Telegram's HTML mode rejects a bare & or < in the body."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _post(url: str, payload: Dict[str, Any], channel: str) -> bool:
    import httpx

    try:
        response = httpx.post(url, json=payload, timeout=settings.NOTIFY_TIMEOUT)
        if response.status_code >= 400:
            # Body, not just the code: Slack answers 200 with "invalid_payload"
            # in the body often enough that the code alone is not diagnostic.
            logger.warning(
                "%s notification rejected (%s): %s",
                channel, response.status_code, response.text[:200],
            )
            return False
        return True
    except Exception as exc:
        logger.warning("%s notification failed: %s: %s", channel, type(exc).__name__, exc)
        return False


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
def send_approval_request(ticket: Dict[str, Any]) -> Dict[str, bool]:
    """Deliver one ticket to every configured channel. Never raises.

    Returns {channel: delivered} so callers and tests can assert on it. An
    unconfigured channel is absent from the result rather than reported as a
    failure — there is nothing wrong with not using Slack.
    """
    results: Dict[str, bool] = {}

    if settings.SLACK_WEBHOOK_URL:
        results["slack"] = _post(
            settings.SLACK_WEBHOOK_URL, _slack_payload(ticket), "Slack"
        )

    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        results["telegram"] = _post(url, _telegram_payload(ticket), "Telegram")

    if results:
        delivered = [c for c, ok in results.items() if ok]
        failed = [c for c, ok in results.items() if not ok]
        logger.info(
            "Approval for %s notified to %s",
            ticket.get("resource_id"), ", ".join(delivered) or "no channel",
        )
        # On the trace, because a notification is a thing the agent did and the
        # operator has no other way to know it happened. Delivery runs on its
        # own thread; the tracer is lock-guarded, so this is safe from here.
        tracer.step(
            APPROVAL,
            (f"Approval for {ticket.get('resource_id')} pushed to "
             f"{', '.join(delivered)}" if delivered
             else f"Could not notify anyone about {ticket.get('resource_id')}"),
            status=INFO if delivered else WARN,
            resource_id=ticket.get("resource_id"),
            detail={"delivered": delivered, "failed": failed or None,
                    "estimated_saving_monthly": ticket.get("estimated_roi")},
        )
    return results


def notify_approval(ticket: Dict[str, Any]) -> Optional[threading.Thread]:
    """Fire-and-forget delivery, off the caller's thread.

    Raising a ticket is part of an audit; telling someone about it is not. A
    webhook that takes ten seconds must not add ten seconds to the audit, and a
    webhook that is down must not fail it.

    Returns the thread so tests can join it. Returns None when no channel is
    configured, which is the normal local case.
    """
    if not configured_channels():
        return None

    # A copy: the ticket keeps being written to after this returns.
    snapshot = dict(ticket)
    thread = threading.Thread(
        target=send_approval_request,
        args=(snapshot,),
        name=f"notify-{snapshot.get('resource_id', 'ticket')}",
        daemon=True,
    )
    thread.start()
    return thread
