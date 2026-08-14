import io

from iwm_0dte_agent.mcp_probe import _read_arguments


def test_read_arguments_returns_literal_string_unchanged():
    assert _read_arguments('{"underlying_symbol": "IWM"}') == '{"underlying_symbol": "IWM"}'


def test_read_arguments_default_empty_object_unchanged():
    assert _read_arguments("{}") == "{}"


def test_read_arguments_dash_reads_from_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"underlying_symbol": "IWM"}\n'))
    assert _read_arguments("-") == '{"underlying_symbol": "IWM"}\n'
