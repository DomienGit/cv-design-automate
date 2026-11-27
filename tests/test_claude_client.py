from __future__ import annotations

import json
import subprocess

import pytest
from pydantic import BaseModel

from cv_app import claude_client
from cv_app.claude_client import (
    ClaudeError,
    ClaudeNotInstalled,
    FakeClaude,
    build_command,
    parse_output,
    run_claude,
    use_runner,
)


class City(BaseModel):
    city: str


@pytest.fixture
def no_sleep():
    delays: list[float] = []
    return delays.append, delays


def test_returns_validated_model(settings, no_sleep):
    sleep, _ = no_sleep
    fake = FakeClaude([{"city": "Warszawa"}])
    with use_runner(fake):
        result = run_claude("Stolica?", City, settings=settings, sleep=sleep)
    assert result == City(city="Warszawa")
    prompt, schema = fake.calls[0]
    assert prompt == "Stolica?"
    assert schema["required"] == ["city"]


def test_dict_schema_returns_raw_json(settings, no_sleep):
    sleep, _ = no_sleep
    with use_runner(FakeClaude([{"a": 1}])):
        assert run_claude("x", {"type": "object"}, settings=settings, sleep=sleep) == {"a": 1}


def test_retries_on_error_and_bad_schema(settings, no_sleep):
    sleep, delays = no_sleep
    fake = FakeClaude([ClaudeError("timeout"), {"town": "?"}, {"city": "Kraków"}])
    with use_runner(fake):
        assert run_claude("x", City, settings=settings, sleep=sleep).city == "Kraków"
    assert len(fake.calls) == 3
    assert delays == [2.0, 4.0]


def test_gives_up_after_retries(settings, no_sleep):
    sleep, _ = no_sleep
    s = settings.model_copy(update={"claude_retries": 1})
    fake = FakeClaude([ClaudeError("a"), ClaudeError("b")])
    with use_runner(fake), pytest.raises(ClaudeError, match="po 2 próbach"):
        run_claude("x", City, settings=s, sleep=sleep)


def test_not_installed_is_not_retried(settings, no_sleep):
    sleep, delays = no_sleep
    fake = FakeClaude([ClaudeNotInstalled("brak")])
    with use_runner(fake), pytest.raises(ClaudeNotInstalled):
        run_claude("x", City, settings=settings, sleep=sleep)
    assert delays == []


def test_build_command(settings):
    s = settings.model_copy(update={"claude_model": "sonnet"})
    cmd = build_command({"type": "object"}, s)
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--json-schema") + 1] == '{"type": "object"}'
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[-2:] == ["--model", "sonnet"]


def test_parse_output_variants():
    ok = {"type": "result", "subtype": "success", "is_error": False, "structured_output": {"a": 1}}
    assert parse_output(json.dumps(ok)) == {"a": 1}
    text_only = {"subtype": "success", "is_error": False, "result": '{"a": 2}'}
    assert parse_output(json.dumps(text_only)) == {"a": 2}
    with pytest.raises(ClaudeError, match="błąd"):
        parse_output(json.dumps({"is_error": True, "result": "limit"}))
    with pytest.raises(ClaudeError):
        parse_output("nie json")


def test_subprocess_runner_uses_stdin_and_empty_workdir(settings, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs, cmd=cmd)
        out = json.dumps({"subtype": "success", "is_error": False, "structured_output": {"c": 3}})
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(claude_client.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(claude_client.subprocess, "run", fake_run)
    result = claude_client.subprocess_runner("dane", {"type": "object"}, settings)
    assert result == {"c": 3}
    assert seen["input"] == "dane"
    assert seen["cwd"] == settings.data_dir / "claude-work"
    assert seen["timeout"] == settings.claude_timeout


def test_subprocess_runner_timeout(settings, monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(claude_client.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(claude_client.subprocess, "run", fake_run)
    with pytest.raises(ClaudeError, match="nie odpowiedział"):
        claude_client.subprocess_runner("x", {}, settings)


def test_subprocess_runner_missing_binary(settings, monkeypatch):
    monkeypatch.setattr(claude_client.shutil, "which", lambda _: None)
    with pytest.raises(ClaudeNotInstalled):
        claude_client.subprocess_runner("x", {}, settings)
