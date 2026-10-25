from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from research_app import diagnostics
from research_app.backends import BackendError
from research_app.models import DiagnosticOutput, ModelConfig, ModelReply, now
from research_app.settings import (
    SettingsError,
    can_call_model,
    configuration_issue,
    load_config,
    resolve_api_key,
    save_config,
)


def test_missing_configuration_uses_defaults_without_creating_files(tmp_path):
    config = load_config(tmp_path)
    assert config.backend == "local_account"
    assert config.local_model == "openai-codex/gpt-6-astra"
    assert config.response_mode == "prompt_json"
    assert not list(tmp_path.iterdir())


def test_keys_are_memory_only_and_page_input_has_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_API_KEY", "environment-secret-value")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-secret-value")
    assert resolve_api_key(" page-secret-value ") == "page-secret-value"
    assert resolve_api_key(" \t ") == "environment-secret-value"
    assert resolve_api_key() == "environment-secret-value"
    assert resolve_api_key(environ={"OPENAI_API_KEY": "do-not-use"}) is None
    config = ModelConfig(
        backend="openai_compatible",
        api_base_url="https://example.test/custom/v1",
        api_model="example-model",
        response_mode="json_schema",
        max_output_tokens=256,
    )
    save_config(config, tmp_path)
    assert load_config(tmp_path) == config
    files = list(tmp_path.iterdir())
    assert [file.name for file in files] == ["model_config.json"]
    raw = files[0].read_text()
    assert set(json.loads(raw)) == set(ModelConfig.model_fields)
    for secret in (
        "environment-secret-value",
        "page-secret-value",
        "unrelated-secret-value",
        "api_key",
    ):
        assert secret not in raw


@pytest.mark.parametrize("extra", [{"api_key": "secret"}, {"unexpected": "value"}])
def test_unknown_configuration_fields_are_rejected(tmp_path, extra):
    path = tmp_path / "model_config.json"
    path.write_text(json.dumps({**ModelConfig().model_dump(), **extra}))
    with pytest.raises(ValidationError):
        load_config(tmp_path)
    assert json.loads(path.read_text()) == {**ModelConfig().model_dump(), **extra}


def test_corrupt_configuration_is_not_silently_replaced(tmp_path):
    path = tmp_path / "model_config.json"
    path.write_text("{broken")
    with pytest.raises(ValidationError):
        load_config(tmp_path)
    assert path.read_text() == "{broken"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.test/v1",
        "https://example.test/v1?api_key=secret",
        "https://example.test/v1#secret",
        "http://example.test/v1",
        "file:///tmp/private",
        "https://example.test:wrong/v1",
        "https://example.test/v1\n",
    ],
)
def test_unsafe_api_urls_never_persist(tmp_path, url):
    baseline = ModelConfig()
    save_config(baseline, tmp_path)
    with pytest.raises(SettingsError):
        save_config(ModelConfig(api_base_url=url), tmp_path)
    assert load_config(tmp_path) == baseline
    assert [file.name for file in tmp_path.iterdir()] == ["model_config.json"]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/v1",
        "http://localhost:8000/v1",
        "http://127.0.0.1:9000/custom",
        "http://[::1]:8000/v1",
    ],
)
def test_complete_api_configuration_allows_keyless_endpoints(url):
    config = ModelConfig(
        backend="openai_compatible", api_base_url=url, api_model="model"
    )
    assert configuration_issue(config) is None
    assert can_call_model(config)


def test_incomplete_api_configuration_disables_only_api_calling(tmp_path):
    config = ModelConfig(backend="openai_compatible")
    save_config(config, tmp_path)
    assert not can_call_model(load_config(tmp_path))
    assert configuration_issue(config) == "API_BASE_URL_REQUIRED"
    config = config.model_copy(update={"api_base_url": "https://example.test/v1"})
    assert configuration_issue(config) == "API_MODEL_REQUIRED"
    assert not can_call_model(config)
    assert can_call_model(ModelConfig())


def test_save_revalidates_unchecked_model_copy(tmp_path):
    invalid = ModelConfig().model_copy(update={"local_model": "not a provider/model"})
    with pytest.raises(ValidationError):
        save_config(invalid, tmp_path)
    assert not list(tmp_path.iterdir())


def test_default_diagnostic_never_constructs_backend_or_writes_configuration(
    tmp_path, monkeypatch, capsys
):
    def forbidden(*args, **kwargs):
        pytest.fail("离线诊断不得创建推理后端")

    monkeypatch.setattr(diagnostics, "build_backend", forbidden)
    monkeypatch.setattr(
        diagnostics.shutil, "which", lambda name: "/private/identity/bin/omp"
    )
    monkeypatch.setenv("RESEARCH_API_KEY", "secret-for-diagnostic")
    assert diagnostics.main(["--data-dir", str(tmp_path)]) == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report == {
        "backend": "local_account",
        "requested_model": "openai-codex/gpt-6-astra",
        "actual_model": None,
        "result": "READY_NOT_CALLED",
    }
    assert output.err == ""
    assert str(tmp_path) not in output.out
    assert "identity" not in output.out
    assert "secret-for-diagnostic" not in output.out
    assert not list(tmp_path.iterdir())


def test_offline_api_diagnostic_does_not_construct_backend(
    tmp_path, monkeypatch, capsys
):
    config = ModelConfig(
        api_base_url="http://localhost:8000/v1", api_model="local-model"
    )
    save_config(config, tmp_path)
    monkeypatch.setattr(
        diagnostics,
        "build_backend",
        lambda *args, **kwargs: pytest.fail("不得调用模型"),
    )
    assert (
        diagnostics.main(
            ["--backend", "openai_compatible", "--data-dir", str(tmp_path)]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["result"] == "READY_NOT_CALLED"
    assert load_config(tmp_path).backend == "local_account"


@pytest.mark.parametrize("backend_name", ["local_account", "openai_compatible"])
def test_live_diagnostic_makes_exactly_one_structured_call(backend_name, monkeypatch):
    config = ModelConfig(
        backend=backend_name,
        api_base_url="http://localhost:8000/v1",
        api_model="test-model",
    )
    model = config.local_model if backend_name == "local_account" else config.api_model
    calls = []

    def complete_json(**kwargs):
        calls.append(kwargs)
        assert kwargs["schema"] is DiagnosticOutput
        assert "tools" not in kwargs
        return ModelReply(
            value=DiagnosticOutput(ok=True),
            requested_model=model,
            actual_model=model,
            backend=backend_name,
            usage={"total_tokens": 10},
            called_at=now(),
            text='{"ok":true,"private":"not exported"}',
        )

    def factory(received_config, api_key=None):
        assert received_config == config
        assert api_key == "page-key"
        return SimpleNamespace(complete_json=complete_json)

    monkeypatch.setattr(diagnostics, "build_backend", factory)
    monkeypatch.setenv("RESEARCH_API_KEY", "environment-key")
    report = diagnostics.run_diagnostic(config, live=True, api_key="page-key")
    assert len(calls) == 1
    assert report == {
        "backend": backend_name,
        "requested_model": model,
        "actual_model": model,
        "result": "SUCCESS",
    }
    assert "private" not in json.dumps(report)
    assert "key" not in json.dumps(report)


def test_live_cli_uses_selected_backend_and_does_not_write_config(
    tmp_path, monkeypatch, capsys
):
    config = ModelConfig(api_base_url="https://example.test/v1", api_model="api-model")
    save_config(config, tmp_path)
    calls = []

    def factory(received_config, api_key=None):
        calls.append(received_config.backend)
        return SimpleNamespace(
            complete_json=lambda **kwargs: ModelReply(
                value=DiagnosticOutput(ok=True),
                requested_model="api-model",
                actual_model="api-model",
                backend="openai_compatible",
                usage=None,
                called_at=now(),
                text='{"ok":true}',
            )
        )

    monkeypatch.setattr(diagnostics, "build_backend", factory)
    assert (
        diagnostics.main(
            ["--backend", "openai_compatible", "--live", "--data-dir", str(tmp_path)]
        )
        == 0
    )
    assert calls == ["openai_compatible"]
    assert json.loads(capsys.readouterr().out)["result"] == "SUCCESS"
    assert load_config(tmp_path) == config


@pytest.mark.parametrize(
    "code,expected",
    [("AUTHENTICATION", "AUTHENTICATION"), ("secret-account-name", "BACKEND_ERROR")],
)
def test_backend_failure_only_reports_fixed_type(code, expected, monkeypatch):
    def factory(*args, **kwargs):
        raise BackendError(
            code,
            "private-account secret-key /Users/private",
            actual_model="other/model",
        )

    monkeypatch.setattr(diagnostics, "build_backend", factory)
    report = diagnostics.run_diagnostic(ModelConfig(), live=True)
    assert report["result"] == expected
    assert report["actual_model"] == "other/model"
    assert "private" not in json.dumps(report)
    assert "secret" not in json.dumps(report)


def test_unexpected_failure_is_sanitized(monkeypatch):
    def factory(*args, **kwargs):
        raise RuntimeError("Authorization: Bearer secret /Users/identity")

    monkeypatch.setattr(diagnostics, "build_backend", factory)
    report = diagnostics.run_diagnostic(ModelConfig(), live=True)
    assert report["result"] == "DIAGNOSTIC_ERROR"
    assert "secret" not in json.dumps(report)
    assert "identity" not in json.dumps(report)


def test_missing_api_config_blocks_even_live_requests(monkeypatch):
    monkeypatch.setattr(
        diagnostics,
        "build_backend",
        lambda *args, **kwargs: pytest.fail("未配置不得调用"),
    )
    report = diagnostics.run_diagnostic(
        ModelConfig(backend="openai_compatible"), live=True
    )
    assert report["result"] == "API_BASE_URL_REQUIRED"


def test_bad_config_and_arguments_do_not_echo_secrets_or_paths(tmp_path, capsys):
    (tmp_path / "model_config.json").write_text('{"api_key":"secret-account"}')
    assert diagnostics.main(["--data-dir", str(tmp_path)]) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["result"] == "CONFIGURATION"
    assert "secret-account" not in output.out
    assert str(tmp_path) not in output.out
    assert output.err == ""
    assert diagnostics.main(["--unknown", "secret-account"]) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)["result"] == "ARGUMENT_ERROR"
    assert "secret-account" not in output.out + output.err


@pytest.mark.parametrize("actual_model", ["different/model", "private-key"])
def test_changed_model_fails_and_key_is_redacted(actual_model, monkeypatch):
    config = ModelConfig()
    reply = ModelReply(
        value=DiagnosticOutput(ok=True),
        requested_model=config.local_model,
        actual_model=actual_model,
        backend=config.backend,
        usage=None,
        called_at=now(),
        text='{"ok":true}',
    )
    monkeypatch.setattr(
        diagnostics,
        "build_backend",
        lambda *args, **kwargs: SimpleNamespace(
            complete_json=lambda **kwargs: reply,
        ),
    )
    report = diagnostics.run_diagnostic(config, live=True, api_key="private-key")
    assert report["result"] == "MODEL_MISMATCH"
    assert report["actual_model"] == (
        None if actual_model == "private-key" else actual_model
    )
    assert "private-key" not in json.dumps(report)


def test_invalid_live_business_schema_does_not_report_success(monkeypatch):
    config = ModelConfig()
    invalid = DiagnosticOutput.model_construct(ok=False)
    reply = ModelReply(
        value=invalid,
        requested_model=config.local_model,
        actual_model=config.local_model,
        backend=config.backend,
        usage=None,
        called_at=now(),
        text='{"ok":false}',
    )
    monkeypatch.setattr(
        diagnostics,
        "build_backend",
        lambda *args, **kwargs: SimpleNamespace(
            complete_json=lambda **kwargs: reply,
        ),
    )
    report = diagnostics.run_diagnostic(config, live=True)
    assert report["result"] == "SCHEMA_VALIDATION"
