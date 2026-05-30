from tvastr.domain import Sensitivity
from tvastr.llm.router import TaskType, build_router


def test_parsing_stays_local(settings):
    router = build_router(settings)
    response, decision = router.run(
        TaskType.LOG_PARSING, "some logs", sensitivity=Sensitivity.SENSITIVE
    )
    assert decision.target == "local"
    assert response.target == "local"


def test_root_cause_goes_to_cloud(settings):
    router = build_router(settings)
    _, decision = router.run(TaskType.ROOT_CAUSE, "why did this fail?")
    assert decision.target == "cloud"


def test_cloud_payload_is_redacted_before_escalation(settings):
    router = build_router(settings)
    secret = "fix bug for jane.doe@example.com key sk-ant-REDACTEDABC123"
    _, decision = router.run(TaskType.FIX_GENERATION, secret, sensitivity=Sensitivity.SENSITIVE)
    assert decision.target == "cloud"
    assert "redacting" in decision.reason
    assert "EMAIL" in decision.reason
