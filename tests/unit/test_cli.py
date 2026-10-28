import pytest

from opspilot.cli import (
    LiveRunNotAuthorizedError,
    build_parser,
    main,
    require_live_authorization,
)
from opspilot.config import LlmMode, get_settings


def test_planned_commands_exit_with_not_implemented(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["backup"]) == 2
    assert "planned for P15+" in capsys.readouterr().err


def test_eval_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["eval"])


def test_parser_requires_a_command() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


@pytest.mark.parametrize(
    "argv",
    [["worker"], ["agent", "run", "--dataset", "dev", "--version", "g1"], ["llm", "smoke"]],
)
def test_provider_calls_need_an_explicit_allow_live(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LLM_MODE", "record")
    get_settings.cache_clear()
    assert main(argv) == 2  # refused before any client, agent or connection is built
    assert "--allow-live" in capsys.readouterr().err


def test_replay_needs_no_authorisation_and_smoke_always_does() -> None:
    require_live_authorization(LlmMode.REPLAY, allow_live=False, command="agent run")
    require_live_authorization(LlmMode.RECORD, allow_live=True, command="agent run")
    with pytest.raises(LiveRunNotAuthorizedError):
        require_live_authorization(LlmMode.LIVE, allow_live=False, command="worker")
