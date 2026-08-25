"""Translation guarantees.

A half-translated interface reads as broken, so the catalogues must stay in
lockstep and every user-facing string must resolve.
"""

import re

import pytest

from app.core.i18n import CATALOG, DEFAULT_LANG, SUPPORTED, normalise, t
from app.tools import rationale


def test_catalogues_have_identical_keys():
    base = set(CATALOG[DEFAULT_LANG])
    for lang in SUPPORTED:
        assert set(CATALOG[lang]) == base, (
            f"'{lang}' differs: missing {base - set(CATALOG[lang])}, "
            f"extra {set(CATALOG[lang]) - base}"
        )


def test_placeholders_match_across_languages():
    """A translation that drops {savings} silently renders the wrong sentence."""
    pattern = re.compile(r"\{(\w+)\}")
    for key, english in CATALOG[DEFAULT_LANG].items():
        expected = set(pattern.findall(english))
        for lang in SUPPORTED:
            assert set(pattern.findall(CATALOG[lang][key])) == expected, (
                f"{lang}:{key} placeholders differ from English"
            )


def test_no_translation_is_left_in_english():
    """Catches keys copied into the Spanish catalogue but never translated."""
    identical = [
        k for k, v in CATALOG["es"].items()
        if v == CATALOG["en"][k] and not _is_legitimately_identical(k, v)
    ]
    assert not identical, f"untranslated Spanish entries: {identical}"


def _is_legitimately_identical(key: str, value: str) -> bool:
    """Product names, code conditions and units are the same in both languages."""
    if key.startswith(("src.", "rule.", "chg.", "trend.")):
        return True
    return value in ("CPU", "Cloud Monitoring", "Artifact Registry")


@pytest.mark.parametrize(
    "given,expected",
    [("es", "es"), ("ES", "es"), ("es-AR", "es"), ("en-US", "en"),
     ("fr", "en"), ("", "en"), (None, "en"), ("nonsense", "en")],
)
def test_language_negotiation(given, expected):
    assert normalise(given) == expected


def test_missing_key_degrades_to_the_key_not_an_exception():
    assert t("es", "does.not.exist") == "does.not.exist"


def test_missing_placeholder_does_not_raise():
    assert t("en", "ev.audit_started")  # no params supplied


def _resource(**over):
    base = {
        "resource_id": "svc", "region": "us-central1", "type": "Cloud Run",
        "cpu_limit": "2", "memory_limit": "4Gi", "min_instances": 2,
        "cpu_utilization": 3.0, "memory_utilization": 8.0,
        "monthly_cost": 300.0, "wasted_cost": 200.0,
        "status": "Idle", "severity": "HIGH", "metrics_source": "monitoring",
    }
    base.update(over)
    return base


@pytest.mark.parametrize("lang", SUPPORTED)
def test_rationale_is_complete_in_every_language(lang):
    why = rationale.explain(_resource(), lang)
    for key in ("verdict", "evidence", "rule", "solution", "expected_result",
                "autonomy", "confidence"):
        assert why.get(key), f"{lang}: missing {key}"
    for row in why["evidence"]:
        assert row["label"] and row["source"]


def test_spanish_rationale_actually_differs_from_english():
    en = rationale.explain(_resource(), "en")
    es = rationale.explain(_resource(), "es")
    assert es["verdict"] != en["verdict"]
    assert es["solution"] != en["solution"]
    assert es["autonomy"]["decision"] != en["autonomy"]["decision"]
    assert es["rule"]["why_it_matters"] != en["rule"]["why_it_matters"]


def test_figures_survive_translation():
    """Translation may reword a value, but never alter its numbers."""
    en = rationale.explain(_resource(), "en")
    es = rationale.explain(_resource(), "es")

    assert en["savings"] == es["savings"]
    assert en["sizing"]["target"] == es["sizing"]["target"]
    assert en["command"] == es["command"]  # gcloud is not translated

    numbers = lambda s: re.findall(r"\d+(?:\.\d+)?", s)
    for a, b in zip(en["evidence"], es["evidence"]):
        assert numbers(a["value"]) == numbers(b["value"]), (
            f"figures changed for '{a['label']}': {a['value']} vs {b['value']}"
        )
    for field in ("expected_result",):
        assert numbers(en[field]) == numbers(es[field]), f"figures changed in {field}"


@pytest.mark.parametrize("lang", SUPPORTED)
def test_every_resource_type_translates(lang):
    samples = [
        _resource(),
        {"resource_id": "d", "type": "Persistent Disk", "zone": "z", "size_gb": 10.0,
         "disk_type": "pd-standard", "monthly_cost": 0.4},
        {"resource_id": "a", "type": "Static IP", "region": "r", "address": "1.2.3.4",
         "monthly_cost": 7.2},
        {"resource_id": "i", "type": "Container Image", "short_id": "sha256:x",
         "repository": "repo", "monthly_cost": 0.1, "created": "2026-01-01T00:00:00Z"},
    ]
    for sample in samples:
        why = rationale.explain(sample, lang)
        assert why["verdict"] and why["autonomy"]["reason"]


def test_status_stays_machine_readable_while_verdict_is_translated():
    """CSS classes and filters key off `status`; only `verdict` is for humans."""
    es = rationale.explain(_resource(), "es")
    assert es["status"] == "Idle", "status must remain a stable English token"
    assert es["verdict"] == "Ocioso"


# --- Persisted text -------------------------------------------------------
def test_events_render_in_the_reader_language_not_the_writer_language():
    """The log is written once but read by operators in either language."""
    from app.tools.memory_tools import MemoryBank, render_event

    bank = MemoryBank(state_file=None)
    event = bank.log_event(key="ev.audit_started", run_id="run_1", count=3)

    assert "Audit run_1 started" in render_event(event, "en")["message"]
    assert "Auditoría run_1 iniciada" in render_event(event, "es")["message"]


def test_free_form_events_survive_rendering():
    from app.tools.memory_tools import MemoryBank, render_event

    bank = MemoryBank(state_file=None)
    event = bank.log_event("a literal message")
    assert render_event(event, "es")["message"] == "a literal message"


def test_approval_tickets_re_render_in_either_language():
    """Regression: tickets froze their text in the audit's language."""
    from app.tools.memory_tools import render_approval

    ticket = {
        "resource_id": "svc",
        "action_key": "act.right_size",
        "change_specs": [
            {"kind": "memory", "from": "2Gi", "to": "512Mi"},
            {"kind": "min_instances", "from": 2, "to": 0},
        ],
        "reason_key": "rule.idle_always_on.why",
        "reason_params": {"min_instances": 2},
    }

    en = render_approval(ticket, "en")
    es = render_approval(ticket, "es")

    assert en["proposed_action"].startswith("Right-size allocation")
    assert es["proposed_action"].startswith("Ajustar la asignación")
    assert "memory 2Gi → 512Mi" in en["proposed_action"]
    assert "memoria 2Gi → 512Mi" in es["proposed_action"]
    assert "billed around the clock" in en["detailed_reason"]
    assert "se facturan las 24 horas" in es["detailed_reason"]


def test_legacy_tickets_without_keys_are_left_alone():
    from app.tools.memory_tools import render_approval

    legacy = {"resource_id": "svc", "proposed_action": "Old text", "detailed_reason": "why"}
    assert render_approval(legacy, "es")["proposed_action"] == "Old text"


@pytest.mark.parametrize("lang", SUPPORTED)
def test_state_endpoint_honours_lang(client, lang):
    body = client.get(f"/api/state?lang={lang}").json()
    assert body["lang"] == lang


def test_unknown_lang_falls_back_without_erroring(client):
    assert client.get("/api/state?lang=klingon").json()["lang"] == "en"


def test_change_specs_are_language_independent():
    """Specs persist in tickets, so they must contain no translated text."""
    from app.tools.rationale import explain

    specs = explain(_resource(), "es")["sizing"]["change_specs"]
    assert specs
    for spec in specs:
        assert spec["kind"] in ("memory", "cpu", "min_instances")
        assert "memoria" not in str(spec), "specs must stay language-neutral"


def test_nested_action_keys_translate_inside_events():
    """An event sentence embedding an action name must translate both parts."""
    from app.tools.memory_tools import MemoryBank, render_event

    bank = MemoryBank(state_file=None)
    event = bank.log_event(
        key="ev.approval_requested",
        action="Delete orphaned persistent disk",
        action_key="act.delete_disk",
        resource="disk-1",
    )
    assert "Delete orphaned persistent disk" in render_event(event, "en")["message"]
    assert "Eliminar disco persistente huérfano" in render_event(event, "es")["message"]


def test_radar_axes_are_catalogue_keys_not_prose():
    """The chart renderer translates them client-side, so they must be keys."""
    from app.tools.gcp_metrics import build_charts

    estate = [{"resource_id": "r", "monthly_cost": 10.0, "wasted_cost": 1.0,
               "status": "Idle", "cpu_utilization": 5.0, "memory_utilization": 5.0}]
    for axis in build_charts(estate, [], inventory=estate)["radar"]:
        assert axis["axis"].startswith("radar."), f"{axis['axis']} is not a key"
        assert axis["axis"] in CATALOG["en"]


def test_state_file_none_means_in_memory_only():
    """Regression: `None` fell through to settings.STATE_FILE, so a throwaway
    bank silently read and wrote the real state file."""
    from app.tools.memory_tools import MemoryBank

    bank = MemoryBank(state_file=None)
    assert bank.state_file is None
    assert bank.snapshot()["events"] == []
    bank.log_event(key="ev.online")  # must not raise while persisting
    assert len(bank.snapshot()["events"]) == 1


def test_client_rendered_keys_exist_in_the_js_catalogue():
    """Regression: radar axes were added to the Python catalogue only, so the
    chart rendered raw keys like 'radar.cost' on screen.

    Any key the server emits for the *client* to translate must live in
    static/js/i18n.js, not app/core/i18n.py.
    """
    import json
    import pathlib
    import re

    js = pathlib.Path("app/web/static/js/i18n.js").read_text(encoding="utf-8")
    js_keys = set(re.findall(r'"([a-z]+\.[a-z_:]+)":', js))

    from app.tools.gcp_metrics import build_charts

    estate = [{"resource_id": "r", "monthly_cost": 10.0, "wasted_cost": 1.0,
               "status": "Idle", "cpu_utilization": 5.0, "memory_utilization": 5.0}]
    emitted = {a["axis"] for a in build_charts(estate, [], inventory=estate)["radar"]}

    missing = emitted - js_keys
    assert not missing, f"keys emitted for the client but absent from i18n.js: {missing}"


def test_status_labels_exist_client_side():
    """The inventory table and donut legends translate status client-side."""
    import pathlib
    import re

    js = pathlib.Path("app/web/static/js/i18n.js").read_text(encoding="utf-8")
    for status in ("Healthy", "Idle", "Oversized", "Orphaned", "Unused", "Untagged"):
        assert re.search(rf'"state\.{status}"', js), f"state.{status} missing from i18n.js"


def test_human_decision_event_translates_every_part():
    """Regression: the verb was stored pre-rendered, producing
    'Un humano approved ...' — half Spanish, half English."""
    from app.tools.memory_tools import MemoryBank, render_event

    bank = MemoryBank(state_file=None)
    event = bank.log_event(
        key="ev.human_decision",
        decision_key="ev.decision.approved",
        action="Right-size",
        action_key="act.right_size",
        resource="svc",
        level="APPROVAL",
    )

    en = render_event(event, "en")["message"]
    es = render_event(event, "es")["message"]
    assert "Human approved" in en
    assert "Un humano aprobó" in es
    assert "approved" not in es, "no English verb may leak into the Spanish sentence"
    assert "Ajustar la asignación" in es
