import pytest

from opspilot.cli import build_parser, main


def test_planned_commands_exit_with_not_implemented(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["eval"]) == 2
    assert "planned for P14" in capsys.readouterr().err


def test_parser_requires_a_command() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
