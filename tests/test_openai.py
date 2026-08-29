"""OpenAI integration tests (task 3.2).

The real Whisper/Vision/embedding clients are tested against a mocked SDK:
assertions cover request arguments and the semantics the Perception agent
depends on (confidence, flagged fragments, error propagation). No network.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openai import OpenAI

from src.agents.perception import (
    TranscriptionError,
    VisionError,
    analyze_image,
    transcribe_voice,
)
from src.config import Settings
from src.integrations.openai import (
    OpenAIEmbedder,
    OpenAITranscriber,
    OpenAIVisionAnalyzer,
    _segment_confidence,
)

CONFIGURED = Settings(openai_api_key="sk-test", openai_embedding_model="text-embedding-3-small")


def _segment(text: str, avg_logprob: float | None) -> SimpleNamespace:
    return SimpleNamespace(text=text, avg_logprob=avg_logprob)


def _fake_client() -> OpenAI:
    """An SDK-shaped mock: attribute chains resolve to MagicMocks."""
    return MagicMock(spec=OpenAI)


def _transcriber(client) -> OpenAITranscriber:
    return OpenAITranscriber(client=client, settings=CONFIGURED)


def test_segment_confidence_maps_logprob_to_unit_range():
    """El avg_logprob de Whisper se mapea a una confianza en [0, 1]."""
    assert _segment_confidence(-0.2) == pytest.approx(0.8)
    assert _segment_confidence(-2.0) == 0.0
    assert _segment_confidence(0.0) == 1.0


def test_transcribe_clean_audio_returns_text_and_high_confidence():
    """Un audio limpio transcribe con texto y confianza alta sin fragmentos."""
    client = _fake_client()
    client.audio.transcriptions.create.return_value = SimpleNamespace(
        text="clavos 2 pulgadas", segments=[_segment("clavos", -0.1), _segment("2 pulgadas", -0.2)]
    )
    result = _transcriber(client).transcribe("audio.ogg")
    assert result.text == "clavos 2 pulgadas"
    assert result.confidence == pytest.approx(0.8)
    assert result.low_confidence_fragments == ()
    call = client.audio.transcriptions.create.call_args
    assert call.kwargs["model"] == "whisper-1"
    assert call.kwargs["file"] == "audio.ogg"
    assert call.kwargs["response_format"] == "verbose_json"


def test_transcribe_noisy_audio_flags_low_confidence_fragments():
    """Un audio ruidoso marca los fragmentos de baja confianza, nunca los descarta."""
    client = _fake_client()
    client.audio.transcriptions.create.return_value = SimpleNamespace(
        text="clavos 2 pulgadas",
        segments=[_segment("clavos", -0.1), _segment("2 pulgadas", -1.5)],
    )
    result = _transcriber(client).transcribe("audio.ogg")
    assert result.confidence == 0.0
    assert result.low_confidence_fragments == ("2 pulgadas",)


def test_transcribe_without_segments_has_full_confidence():
    """Sin segmentos disponibles la confianza es plena (1.0)."""
    client = _fake_client()
    client.audio.transcriptions.create.return_value = SimpleNamespace(text="hola", segments=None)
    result = _transcriber(client).transcribe("audio.ogg")
    assert result.confidence == 1.0
    assert result.low_confidence_fragments == ()


def test_transcribe_propagates_provider_errors_as_transcription_error():
    """A provider error propagates as TranscriptionError through perception."""
    client = _fake_client()
    client.audio.transcriptions.create.side_effect = RuntimeError("network down")
    with pytest.raises(TranscriptionError):
        transcribe_voice(_transcriber(client), "audio.ogg")


def test_analyze_image_returns_text_with_stop_finish():
    """Una imagen analizada devuelve el texto con confianza plena al finalizar normal."""
    client = _fake_client()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="caja de clavos"), finish_reason="stop")
        ]
    )
    result = OpenAIVisionAnalyzer(client=client, settings=CONFIGURED).analyze(
        "https://img/caja.jpg", "¿qué hay?"
    )
    assert result.text == "caja de clavos"
    assert result.confidence == 1.0
    messages = client.chat.completions.create.call_args.kwargs["messages"]
    assert messages[0]["content"][1]["image_url"]["url"] == "https://img/caja.jpg"


def test_analyze_image_suspect_finish_lowers_confidence():
    """Un cierre anómalo (length) baja la confianza del análisis."""
    client = _fake_client()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="parcial"), finish_reason="length")
        ]
    )
    result = OpenAIVisionAnalyzer(client=client, settings=CONFIGURED).analyze(
        "https://img/x.jpg", "describe"
    )
    assert result.confidence == 0.0


def test_analyze_image_raises_vision_error_on_provider_failure():
    """A vision provider failure propagates as VisionError through perception."""
    client = _fake_client()
    client.chat.completions.create.side_effect = RuntimeError("timeout")
    analyzer = OpenAIVisionAnalyzer(client=client, settings=CONFIGURED)
    with pytest.raises(VisionError):
        analyze_image(analyzer, "https://img/x.jpg", "x")


def test_embed_preserves_input_order_and_passes_model_dimensions():
    """El embedder conserva el orden de entrada y pasa modelo y dimensiones."""
    client = _fake_client()
    client.embeddings.create.return_value = SimpleNamespace(
        data=[
            SimpleNamespace(index=1, embedding=[0.2, 0.3]),
            SimpleNamespace(index=0, embedding=[0.9, 0.1]),
        ]
    )
    vectors = OpenAIEmbedder(client=client, settings=CONFIGURED).embed(["clavo", "cemento"])
    assert vectors[0] == [0.9, 0.1]
    assert vectors[1] == [0.2, 0.3]
    call = client.embeddings.create.call_args
    assert call.kwargs["model"] == "text-embedding-3-small"
    assert call.kwargs["input"] == ["clavo", "cemento"]
    assert call.kwargs["dimensions"] == 1536
