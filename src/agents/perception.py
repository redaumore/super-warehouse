"""Perception agent: speech-to-text and image understanding.

Owns the two sensory tools of the order pipeline — transcribing voice notes
(Whisper STT) and understanding photos (GPT-4o Vision). The whatsapp-order-
intake spec fixes the semantics this module enforces:

- clean audio → a usable transcript with the flagged fragments empty;
- noisy audio → a best-effort transcript where any low-confidence fragments are
  FLAGGED for downstream disambiguation, never silently dropped;
- outright failure (exception, empty transcript) → ``TranscriptionError`` so the
  caller can ask the customer to resend as text or a fresh voice note.

The OpenAI-backed implementations land with the integrations layer (Phase 3,
task 3.2). This phase defines the mockable provider interface (``Transcriber`` /
``VisionAnalyzer``) so every unit test here uses a fake provider and never
touches the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class PerceptionError(Exception):
    """Base error for perception failures (STT or vision)."""


class TranscriptionError(PerceptionError):
    """A voice note could not be transcribed — ask the customer to resend."""


class VisionError(PerceptionError):
    """An image could not be analyzed."""


@dataclass(frozen=True)
class TranscriptionResult:
    """Outcome of transcribing one voice note.

    ``confidence`` is in [0, 1]. ``low_confidence_fragments`` carries the parts
    the provider is not sure about; the caller must confirm them instead of
    guessing (spec: partial transcription is confirmed, not quoted).
    """

    text: str
    confidence: float
    low_confidence_fragments: tuple[str, ...] = ()


@dataclass(frozen=True)
class VisionResult:
    """Outcome of analyzing one image (photo of a remito, barcode, catalog…)."""

    text: str
    confidence: float


class Transcriber(Protocol):
    """Mockable speech-to-text boundary (real impl: OpenAI Whisper, task 3.2)."""

    def transcribe(self, audio_path: str) -> TranscriptionResult:
        """Transcribe the audio file at ``audio_path``."""
        ...


class VisionAnalyzer(Protocol):
    """Mockable image-understanding boundary (real impl: GPT-4o Vision, task 3.2)."""

    def analyze(self, image_url: str, prompt: str) -> VisionResult:
        """Analyze the image at ``image_url`` guided by ``prompt``."""
        ...


DEFAULT_VISION_PROMPT = (
    "Describe the products and quantities visible in this image. "
    "If it is a document (remito, invoice or price list), extract the line items "
    "with quantities and any prices; flag anything illegible."
)


def transcribe_voice(transcriber: Transcriber, audio_path: str) -> TranscriptionResult:
    """Transcribe a voice note, enforcing the failure semantics of the spec.

    Raises ``TranscriptionError`` only on outright failure (provider error or an
    empty transcript). Low-confidence output is returned with its fragments
    flagged so the caller can prompt the customer to confirm, never guessed.
    """
    try:
        result = transcriber.transcribe(audio_path)
    except Exception as exc:
        raise TranscriptionError(f"audio could not be transcribed: {exc}") from exc
    if not result.text.strip():
        raise TranscriptionError("audio produced no transcript")
    return result


def analyze_image(
    analyzer: VisionAnalyzer,
    image_url: str,
    prompt: str | None = None,
) -> VisionResult:
    """Analyze an image, raising ``VisionError`` when nothing usable comes back."""
    try:
        result = analyzer.analyze(image_url, prompt or DEFAULT_VISION_PROMPT)
    except Exception as exc:
        raise VisionError(f"image could not be analyzed: {exc}") from exc
    if not result.text.strip():
        raise VisionError("image analysis produced no description")
    return result
