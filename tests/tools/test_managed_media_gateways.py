import sys
import types
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"


def _load_tool_module(module_name: str, filename: str):
    spec = spec_from_file_location(module_name, TOOLS_DIR / filename)
    assert spec and spec.loader
    module = module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _restore_tool_and_agent_modules():
    original_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "tools"
        or name.startswith("tools.")
        or name == "agent"
        or name.startswith("agent.")
        or name in {"fal_client", "openai"}
    }
    try:
        yield
    finally:
        for name in list(sys.modules):
            if (
                name == "tools"
                or name.startswith("tools.")
                or name == "agent"
                or name.startswith("agent.")
                or name in {"fal_client", "openai"}
            ):
                sys.modules.pop(name, None)
        sys.modules.update(original_modules)


def _install_fake_tools_package():
    tools_package = types.ModuleType("tools")
    tools_package.__path__ = [str(TOOLS_DIR)]  # type: ignore[attr-defined]
    sys.modules["tools"] = tools_package
    sys.modules["tools.debug_helpers"] = types.SimpleNamespace(
        DebugSession=lambda *args, **kwargs: types.SimpleNamespace(
            active=False,
            session_id="debug-session",
            log_call=lambda *a, **k: None,
            save=lambda: None,
            get_session_info=lambda: {},
        )
    )


def _install_fake_openai_module(captured, transcription_response=None):
    class FakeSpeechResponse:
        def stream_to_file(self, output_path):
            captured["stream_to_file"] = output_path

    class FakeOpenAI:
        def __init__(self, api_key, base_url, **kwargs):
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            captured["client_kwargs"] = kwargs
            captured["close_calls"] = captured.get("close_calls", 0)

            def create_speech(**kwargs):
                captured["speech_kwargs"] = kwargs
                return FakeSpeechResponse()

            def create_transcription(**kwargs):
                captured["transcription_kwargs"] = kwargs
                return transcription_response

            self.audio = types.SimpleNamespace(
                speech=types.SimpleNamespace(
                    create=create_speech
                ),
                transcriptions=types.SimpleNamespace(
                    create=create_transcription
                ),
            )

        def close(self):
            captured["close_calls"] += 1

    fake_module = types.SimpleNamespace(
        OpenAI=FakeOpenAI,
        APIError=Exception,
        APIConnectionError=Exception,
        APITimeoutError=Exception,
        BadRequestError=type("BadRequestError", (Exception,), {}),
    )
    sys.modules["openai"] = fake_module


def test_openai_tts_accepts_direct_openai_api_key(monkeypatch, tmp_path):
    captured = {}
    _install_fake_tools_package()
    _install_fake_openai_module(captured)
    monkeypatch.delenv("VOICE_TOOLS_OPENAI_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-direct-key")

    tts_tool = _load_tool_module("tools.tts_tool", "tts_tool.py")
    output_path = tmp_path / "speech.mp3"
    tts_tool._generate_openai_tts("hello world", str(output_path), {"openai": {}})

    assert captured["api_key"] == "openai-direct-key"
    assert captured["base_url"] == "https://api.openai.com/v1"
    assert captured["close_calls"] == 1


def test_transcription_uses_model_specific_response_formats(monkeypatch, tmp_path):
    whisper_capture = {}
    _install_fake_tools_package()
    _install_fake_openai_module(whisper_capture, transcription_response="hello from whisper")
    monkeypatch.setenv("CLOVER_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("stt:\n  provider: openai\n")
    monkeypatch.delenv("VOICE_TOOLS_OPENAI_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-direct-key")

    transcription_tools = _load_tool_module(
        "tools.transcription_tools",
        "transcription_tools.py",
    )
    transcription_tools._load_stt_config = lambda: {"provider": "openai"}
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"RIFF0000WAVEfmt ")

    whisper_result = transcription_tools.transcribe_audio(str(audio_path), model="whisper-1")
    assert whisper_result["success"] is True
    assert whisper_capture["api_key"] == "openai-direct-key"
    assert whisper_capture["transcription_kwargs"]["response_format"] == "text"
    assert whisper_capture["close_calls"] == 1

    json_capture = {}
    _install_fake_openai_module(
        json_capture,
        transcription_response=types.SimpleNamespace(text="hello from gpt-4o"),
    )
    transcription_tools = _load_tool_module(
        "tools.transcription_tools",
        "transcription_tools.py",
    )

    json_result = transcription_tools.transcribe_audio(
        str(audio_path),
        model="gpt-4o-mini-transcribe",
    )
    assert json_result["success"] is True
    assert json_result["transcript"] == "hello from gpt-4o"
    assert json_capture["transcription_kwargs"]["response_format"] == "json"
    assert json_capture["close_calls"] == 1


PLUGINS_DIR = Path(__file__).resolve().parents[2] / "plugins"


def test_video_gen_happy_horse_uses_alibaba_namespace():
    """Verify the happy-horse family uses alibaba/ not fal-ai/ endpoints."""
    _install_fake_tools_package()

    # Load just the plugin module to check the catalog
    plugin_init = PLUGINS_DIR / "video_gen" / "fal" / "__init__.py"

    agent_dir = Path(__file__).resolve().parents[2] / "agent"
    spec = spec_from_file_location(
        "agent.video_gen_provider",
        agent_dir / "video_gen_provider.py",
    )
    mod = module_from_spec(spec)
    sys.modules["agent.video_gen_provider"] = mod
    spec.loader.exec_module(mod)

    spec = spec_from_file_location("plugins.video_gen.fal", plugin_init)
    plugin_mod = module_from_spec(spec)
    sys.modules["plugins.video_gen.fal"] = plugin_mod
    spec.loader.exec_module(plugin_mod)

    hh = plugin_mod.FAL_FAMILIES["happy-horse"]
    assert hh["text_endpoint"] == "alibaba/happy-horse/text-to-video"
    assert hh["image_endpoint"] == "alibaba/happy-horse/image-to-video"
