"""Perception agent tests (task 2.2).

The provider interface is mockable: every test uses a fake ``Transcriber`` /
``VisionAnalyzer`` and never touches the network. Covers the whatsapp-order-
intake spec semantics: clean audio transcribed, noisy audio flagged (not
dropped), outright failure raised as ``TranscriptionError``.
"""

from __future__ import annotations

import pytest

from src.agents.perception import (
    DEFAULT_VISION_PROMPT,
    PerceptionError,
    TranscriptionError,
    TranscriptionResult,
    VisionError,
    VisionResult,
    analyze_image,
    transcribe_voice,
)


class FakeTranscriber:
    """Configurable fake STT provider."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.called_with = None

    def transcribe(self, audio_path):
        self.called_with = audio_path
        if self.error is not None:
            raise self.error
        return self.result


class FakeVisionAnalyzer:
    """Configurable fake vision provider."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.called_with = None

    def analyze(self, image_url, prompt):
        self.called_with = (image_url, prompt)
        if self.error is not None:
            raise self.error
        return self.result


def test_transcribe_clean_audio_returns_text():
    """Audio limpio se transcribe a texto utilizable sin fragmentos marcados.

    Spec: clean audio transcribed → usable transcript, nothing flagged.
    """
    provider = FakeTranscriber(
        result=TranscriptionResult(text="necesito 10 clavos de 2 pulgadas", confidence=0.97)
    )
    result = transcribe_voice(provider, "/tmp/nota.ogg")
    assert result.text == "necesito 10 clavos de 2 pulgadas"
    assert result.confidence == 0.97
    assert result.low_confidence_fragments == ()
    assert provider.called_with == "/tmp/nota.ogg"


def test_transcribe_noisy_audio_flags_fragments_not_dropped():
    """Audio ruidoso se transcribe igual y marca los fragmentos de baja confianza.

    Spec: noisy audio → best-effort transcript with fragments flagged.
    """
    provider = FakeTranscriber(
        result=TranscriptionResult(
            text="quiero un taladro y algo de cinta",
            confidence=0.55,
            low_confidence_fragments=("algo de cinta",),
        )
    )
    result = transcribe_voice(provider, "/tmp/ruidosa.ogg")
    assert "taladro" in result.text
    assert result.low_confidence_fragments == ("algo de cinta",)


def test_transcribe_provider_error_raises_transcription_error():
    """Un fallo del proveedor de transcripción lanza TranscriptionError.

    Spec: transcription fails outright → TranscriptionError, not a guess.
    """
    provider = FakeTranscriber(error=RuntimeError("silent audio"))
    with pytest.raises(TranscriptionError, match="could not be transcribed"):
        transcribe_voice(provider, "/tmp/vacia.ogg")


def test_transcribe_empty_transcript_raises():
    """Una transcripción vacía es un fallo, no un éxito silencioso.

    An empty transcript is an outright failure, never a silent success.
    """
    provider = FakeTranscriber(result=TranscriptionResult(text="   ", confidence=0.9))
    with pytest.raises(TranscriptionError, match="no transcript"):
        transcribe_voice(provider, "/tmp/silencio.ogg")


def test_transcription_error_is_a_perception_error():
    """TranscriptionError es un subtipo de PerceptionError."""
    assert issubclass(TranscriptionError, PerceptionError)


def test_analyze_image_returns_vision_text():
    """Analizar una imagen devuelve el texto descriptivo con su confianza."""
    provider = FakeVisionAnalyzer(
        result=VisionResult(text='remito con 3 items: clavos 2", tornillos M6', confidence=0.9)
    )
    result = analyze_image(provider, "https://cdn/media/remito1.jpg")
    assert "clavos" in result.text
    assert result.confidence == 0.9
    url, prompt = provider.called_with
    assert url == "https://cdn/media/remito1.jpg"
    assert prompt == DEFAULT_VISION_PROMPT


def test_analyze_image_custom_prompt_forwarded():
    """Un prompt personalizado se reenvía al proveedor de visión."""
    provider = FakeVisionAnalyzer(result=VisionResult(text="un taladro", confidence=0.8))
    analyze_image(provider, "https://cdn/media/barcode.jpg", prompt="read the barcode number")
    assert provider.called_with[1] == "read the barcode number"


def test_analyze_image_provider_error_raises_vision_error():
    """Un fallo del proveedor de visión lanza VisionError."""
    provider = FakeVisionAnalyzer(error=RuntimeError("invalid image"))
    with pytest.raises(VisionError, match="could not be analyzed"):
        analyze_image(provider, "https://cdn/media/broken.jpg")


def test_analyze_image_empty_description_raises():
    """Una imagen sin descripción lanza VisionError."""
    provider = FakeVisionAnalyzer(result=VisionResult(text="", confidence=0.9))
    with pytest.raises(VisionError, match="no description"):
        analyze_image(provider, "https://cdn/media/empty.jpg")
