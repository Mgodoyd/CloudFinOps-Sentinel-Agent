"""Approvals have to go and find a person.

The agent runs hourly while nobody is watching. A ticket that only exists in a
dashboard nobody has open is a human-in-the-loop that depends on someone walking
past it, so a raised ticket is pushed to whichever chat the team actually reads.

What matters here is the failure behaviour as much as the delivery: a webhook
that is down, slow or misconfigured must cost the notification and nothing else.
The finding is already persisted by the time this runs.
"""

from typing import Any, Dict, List

import pytest

from app.core.config import settings
from app.tools import gcp_remediator as r
from app.tools import notifications
from app.tools.memory_tools import memory_bank

TICKET = {
    "resource_id": "checkout-api",
    "proposed_action": "Right-size allocation → 1 vCPU / 2Gi / min-instances 0",
    "detailed_reason": "2 warm instances at 20.4% CPU bill around the clock.",
    "estimated_roi": 148.15,
    "severity": "HIGH",
    "target_shape": {"memory": "2Gi", "cpu": "1", "min_instances": 0},
}


class Recorder:
    """Stands in for httpx.post and records what would have been sent."""

    def __init__(self, status: int = 200, raises: Exception = None):
        self.calls: List[Dict[str, Any]] = []
        self.status = status
        self.raises = raises

    def __call__(self, url, json=None, timeout=None, **kwargs):
        if self.raises:
            raise self.raises
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return type("Response", (), {"status_code": self.status, "text": "recorded"})()


@pytest.fixture
def post(monkeypatch):
    import httpx

    recorder = Recorder()
    monkeypatch.setattr(httpx, "post", recorder)
    return recorder


@pytest.fixture
def slack(monkeypatch):
    monkeypatch.setattr(settings, "SLACK_WEBHOOK_URL", "https://hooks.slack.test/T/B/X")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "")


@pytest.fixture
def telegram(monkeypatch):
    monkeypatch.setattr(settings, "SLACK_WEBHOOK_URL", "")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "12345:AAtoken")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "-100999")


# --- 1. silence is the default -------------------------------------------
def test_nothing_is_sent_when_no_channel_is_configured(post):
    assert notifications.configured_channels() == []
    assert notifications.send_approval_request(TICKET) == {}
    assert not post.calls, "an unconfigured channel is skipped, not attempted"


def test_notify_returns_none_rather_than_starting_a_thread():
    assert notifications.notify_approval(TICKET) is None


def test_telegram_needs_both_halves(monkeypatch, post):
    """A token with no chat id cannot address anyone; that is not configured."""
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "12345:AAtoken")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "")
    assert "telegram" not in notifications.configured_channels()
    assert not post.calls


# --- 2. what each channel actually receives -------------------------------
def test_slack_carries_the_money_the_resource_and_the_shape(slack, post):
    assert notifications.send_approval_request(TICKET) == {"slack": True}

    call = post.calls[0]
    assert call["url"] == "https://hooks.slack.test/T/B/X"
    body = str(call["json"])
    assert "148.15" in body
    assert "checkout-api" in body
    assert "1 vCPU / 2Gi / min-instances 0" in body


def test_slack_sends_a_text_fallback_alongside_the_blocks(slack, post):
    """Slack warns on block-only payloads, and clients that cannot render
    blocks show nothing at all."""
    notifications.send_approval_request(TICKET)
    payload = post.calls[0]["json"]
    assert payload["text"] and payload["blocks"]


def test_telegram_addresses_the_configured_chat(telegram, post):
    assert notifications.send_approval_request(TICKET) == {"telegram": True}

    call = post.calls[0]
    assert call["url"] == "https://api.telegram.org/bot12345:AAtoken/sendMessage"
    assert call["json"]["chat_id"] == "-100999"
    assert "148.15" in call["json"]["text"]


def test_telegram_escapes_html_in_untrusted_text(telegram, post):
    """A resource name is attacker-influenced; unescaped it breaks the message
    at best and injects markup at worst."""
    notifications.send_approval_request(
        {**TICKET, "resource_id": "svc<script>&co", "target_shape": {}}
    )
    text = post.calls[0]["json"]["text"]
    assert "&lt;script&gt;" in text and "&amp;co" in text
    assert "<script>" not in text


def test_both_channels_fire_when_both_are_configured(monkeypatch, post):
    monkeypatch.setattr(settings, "SLACK_WEBHOOK_URL", "https://hooks.slack.test/T/B/X")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "12345:AAtoken")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "-100999")

    assert notifications.send_approval_request(TICKET) == {"slack": True, "telegram": True}
    assert len(post.calls) == 2


def test_the_shape_is_not_repeated_when_the_headline_already_says_it(slack, post):
    """Every line in a chat message costs attention."""
    notifications.send_approval_request(TICKET)
    assert str(post.calls[0]["json"]).count("1 vCPU / 2Gi / min-instances 0") == 1


def test_a_link_back_to_the_deck_is_included_when_known(monkeypatch, slack, post):
    monkeypatch.setattr(settings, "DASHBOARD_URL", "https://sentinel.example.run.app/")
    notifications.send_approval_request(TICKET)
    assert "https://sentinel.example.run.app" in str(post.calls[0]["json"])


# --- 3. failure costs the message, never the finding ----------------------
def test_a_rejected_webhook_is_reported_not_raised(monkeypatch, slack):
    import httpx

    monkeypatch.setattr(httpx, "post", Recorder(status=404))
    assert notifications.send_approval_request(TICKET) == {"slack": False}


def test_an_unreachable_host_is_reported_not_raised(monkeypatch, slack):
    import httpx

    monkeypatch.setattr(httpx, "post", Recorder(raises=OSError("no route to host")))
    assert notifications.send_approval_request(TICKET) == {"slack": False}


def test_one_dead_channel_does_not_stop_the_other(monkeypatch, post):
    import httpx

    monkeypatch.setattr(settings, "SLACK_WEBHOOK_URL", "https://hooks.slack.test/T/B/X")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "12345:AAtoken")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "-100999")

    def slack_is_down(url, json=None, timeout=None, **kwargs):
        if "slack" in url:
            raise OSError("down")
        return post(url, json=json, timeout=timeout)

    monkeypatch.setattr(httpx, "post", slack_is_down)
    assert notifications.send_approval_request(TICKET) == {"slack": False, "telegram": True}


def test_delivery_is_bounded_by_a_timeout(slack, post):
    """A hanging webhook must not hold an audit open indefinitely."""
    notifications.send_approval_request(TICKET)
    assert post.calls[0]["timeout"] == settings.NOTIFY_TIMEOUT


# --- 4. wired into the ticket that matters --------------------------------
def test_raising_a_ticket_notifies(monkeypatch, slack, post):
    r.request_human_approval(
        "checkout-api", "Right-size allocation", 148.15,
        target_memory="2Gi", severity="HIGH",
    )
    thread = None
    for t in __import__("threading").enumerate():
        if t.name.startswith("notify-"):
            thread = t
    if thread:
        thread.join(timeout=5)

    assert post.calls, "a Level 2 ticket has to reach a human somehow"
    assert "checkout-api" in str(post.calls[0]["json"])


def test_a_dead_webhook_does_not_lose_the_ticket(monkeypatch, slack):
    import httpx

    monkeypatch.setattr(httpx, "post", Recorder(raises=OSError("down")))
    r.request_human_approval("svc-z", "Resize", 99.0, target_memory="512Mi")

    assert memory_bank.has_pending_approval("svc-z"), (
        "the ticket is persisted before anyone is told; a failed notification "
        "must never take the finding with it"
    )


# --- 5. the operator can see that it happened ------------------------------
def test_a_delivery_lands_on_the_trace(slack, post):
    """Without this the feature is invisible: nothing in the UI says a
    notification was sent, so nobody can tell it works — including in a demo."""
    from app.core.trace import tracer

    tracer.clear()
    notifications.send_approval_request(TICKET)

    steps = [s for s in tracer.steps() if s["phase"] == "APPROVAL"]
    assert steps, "a notification is something the agent did; it belongs on the trace"
    assert "slack" in steps[-1]["message"]
    assert steps[-1]["detail"]["delivered"] == ["slack"]


def test_a_failed_delivery_says_so_on_the_trace(monkeypatch, slack):
    from app.core.trace import tracer
    import httpx

    monkeypatch.setattr(httpx, "post", Recorder(raises=OSError("down")))
    tracer.clear()
    notifications.send_approval_request(TICKET)

    step = [s for s in tracer.steps() if s["phase"] == "APPROVAL"][-1]
    assert "Could not notify" in step["message"]
    assert step["detail"]["failed"] == ["slack"]


def test_silence_is_not_traced(post):
    """No channel configured is a choice, not an event."""
    from app.core.trace import tracer

    tracer.clear()
    notifications.send_approval_request(TICKET)
    assert not [s for s in tracer.steps() if s["phase"] == "APPROVAL"]


# --- 6. preflight says whether anyone will hear about it --------------------
def test_preflight_warns_when_nobody_will_be_told():
    from app.tools.preflight import run_preflight

    check = [c for c in run_preflight()["checks"] if c["name"] == "Notifications"][0]
    assert check["status"] == "warn"
    assert "TELEGRAM_BOT_TOKEN" in check["fix"]


def test_preflight_names_the_configured_channels(telegram):
    from app.tools.preflight import run_preflight

    check = [c for c in run_preflight()["checks"] if c["name"] == "Notifications"][0]
    assert check["status"] == "ok"
    assert "telegram" in check["detail"]
