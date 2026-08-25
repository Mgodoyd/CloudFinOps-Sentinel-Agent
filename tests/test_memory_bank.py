from app.tools.memory_tools import memory_bank


def test_check_history_finds_remediated_resource():
    """Regression: log_remediation used to drop resource_id, so check_history
    always returned None and the agent could loop on the same resource."""
    assert memory_bank.check_history("svc-a")["found"] is False

    memory_bank.log_remediation(
        event_id="resize_svc-a", resource_id="svc-a", action="resize", savings=12.5
    )

    history = memory_bank.check_history("svc-a")
    assert history["found"] is True
    assert history["last_action"] == "resize"
    assert history["times_remediated"] == 1


def test_resource_id_inferred_from_event_id():
    memory_bank.log_remediation(event_id="purge_image-9", action="purge", savings=5.0)
    assert memory_bank.check_history("image-9")["found"] is True


def test_total_savings_accumulates():
    memory_bank.log_remediation("a_x", "resize", 10.0, resource_id="x")
    memory_bank.log_remediation("a_y", "purge", 5.25, resource_id="y")
    assert memory_bank.total_savings() == 15.25


def test_approval_lifecycle():
    memory_bank.add_approval(
        {"resource_id": "svc-b", "proposed_action": "Resize", "estimated_roi": 60.0}
    )
    assert memory_bank.has_pending_approval("svc-b") is True

    resolved = memory_bank.resolve_approval("svc-b", "APPROVED")
    assert resolved["status"] == "APPROVED"
    assert resolved["resolved_at"] is not None
    assert memory_bank.has_pending_approval("svc-b") is False
    assert memory_bank.resolve_approval("svc-b", "APPROVED") is None


def test_state_survives_restart(tmp_path):
    from app.tools.memory_tools import MemoryBank

    path = str(tmp_path / "bank.json")
    first = MemoryBank(state_file=path)
    first.log_remediation("e_1", "resize", 20.0, resource_id="svc-c")

    second = MemoryBank(state_file=path)
    assert second.check_history("svc-c")["found"] is True


def test_event_log_is_bounded():
    for i in range(260):
        memory_bank.log_event(f"event {i}")
    assert len(memory_bank.snapshot()["events"]) <= 200
