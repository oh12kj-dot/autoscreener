from typer.testing import CliRunner

from autoscreener import cli


def test_external_information_collection_commands_forward_options(monkeypatch):
    captured: dict[str, tuple[list[str] | None, int]] = {}

    def record(name: str):
        def _collector(*, symbols=None, limit=300):
            captured[name] = (symbols, limit)
            return {"new_rows": 1}

        return _collector

    monkeypatch.setattr(cli, "collect_concentration", record("concentration"))
    monkeypatch.setattr(cli, "collect_guidance", record("guidance"))
    monkeypatch.setattr(cli, "collect_filing_sections", record("sections"))
    monkeypatch.setattr(cli, "collect_litigation", record("litigation"))

    runner = CliRunner()
    for command, name in (
        ("collect-concentration", "concentration"),
        ("collect-guidance", "guidance"),
        ("collect-filing-sections", "sections"),
        ("collect-litigation", "litigation"),
    ):
        result = runner.invoke(cli.app, [command, "--symbols", "abc, def", "--limit", "7"])
        assert result.exit_code == 0, result.output
        assert "new_rows: 1" in result.output
        assert captured[name] == (["ABC", "DEF"], 7)
