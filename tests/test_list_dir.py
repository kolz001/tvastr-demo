from tvastr.agent.context import AgentContext
from tvastr.agent.tools import list_dir
from tvastr.config import Settings
from tvastr.integrations import build_notifier
from tvastr.integrations.github import MockGitHubClient
from tvastr.llm.router import build_router


def test_mock_list_dir_returns_entries():
    host = MockGitHubClient()
    entries = host.list_dir("llama_index/core/memory")
    assert isinstance(entries, list)
    assert all(isinstance(e, str) for e in entries)


def test_list_dir_tool_wraps_host():
    settings = Settings(use_mocks=True, audit_backend="memory")
    ctx = AgentContext(
        router=build_router(settings),
        code_host=MockGitHubClient(),
        notifier=build_notifier(settings),
    )
    entries = list_dir(ctx, "llama_index/core")
    assert isinstance(entries, list)


def test_list_dir_tool_degrades_on_error():
    class _Boom:
        def list_dir(self, path):
            raise RuntimeError("nope")

    class _Ctx:
        code_host = _Boom()

    assert list_dir(_Ctx(), "x") == []
