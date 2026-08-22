"""OpenAI integrations (task 3.2): Whisper STT, GPT-4o Vision, embeddings.

Implements the mockable provider protocols the Perception agent defines
(``Transcriber`` / ``VisionAnalyzer`` in ``src.agents.perception``) with the
real OpenAI SDK, plus ``OpenAIEmbedder`` for catalog/query embeddings.

The SDK client is injected in the constructor so unit tests mock the boundary
without touching the network; when omitted it is built from settings. The
whisper transcription requests ``verbose_json`` so segment-level logprobs can
drive the perception semantics: low-confidence fragments are flagged (never
silently dropped) and the overall confidence is derived from the worst segment.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, cast

from openai import OpenAI

from src.agents.perception import TranscriptionResult, VisionResult
from src.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _segment_confidence(avg_logprob: float) -> float:
    """Map a Whisper segment ``avg_logprob`` (roughly [-3, 0]) to [0, 1]."""
    return max(0.0, min(1.0, 1.0 + avg_logprob))


class OpenAITranscriber:
    """Whisper speech-to-text client implementing the ``Transcriber`` protocol."""

    def __init__(
        self,
        client: OpenAI | None = None,
        *,
        model: str = "whisper-1",
        low_confidence_logprob: float = -1.0,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client or OpenAI(api_key=self.settings.openai_api_key or None)
        self.model = model
        self.low_confidence_logprob = low_confidence_logprob

    def transcribe(self, audio_path: str) -> TranscriptionResult:
        """Transcribe the audio file, flagging low-confidence fragments."""
        response = self._client.audio.transcriptions.create(
            model=self.model,
            # The SDK overloads `file` with PathLike/IO unions; a plain path
            # string is accepted at runtime but needs the Any cast to match.
            file=cast(Any, audio_path),
            response_format="verbose_json",
        )
        text = response.text or ""
        segments = response.segments or []
        fragments = tuple(
            segment.text.strip()
            for segment in segments
            if segment.avg_logprob is not None
            and segment.avg_logprob < self.low_confidence_logprob
            and segment.text.strip()
        )
        logprobs = [segment.avg_logprob for segment in segments if segment.avg_logprob is not None]
        confidence = _segment_confidence(min(logprobs)) if logprobs else 1.0
        return TranscriptionResult(text=text, confidence=confidence, low_confidence_fragments=fragments)


class OpenAIVisionAnalyzer:
    """GPT-4o Vision client implementing the ``VisionAnalyzer`` protocol."""

    def __init__(
        self,
        client: OpenAI | None = None,
        *,
        model: str = "gpt-4o",
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client or OpenAI(api_key=self.settings.openai_api_key or None)
        self.model = model

    def analyze(self, image_url: str, prompt: str) -> VisionResult:
        """Analyze the image at ``image_url`` guided by ``prompt``."""
        completion = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
        )
        text = (completion.choices[0].message.content or "").strip()
        finish_reason = completion.choices[0].finish_reason
        # "stop" means the model finished normally; anything else (length,
        # content_filter, …) means the output is suspect.
        confidence = 1.0 if finish_reason == "stop" else 0.0
        return VisionResult(text=text, confidence=confidence)


class OpenAIEmbedder:
    """Embedding client: maps texts to fixed-dimension vectors."""

    def __init__(
        self,
        client: OpenAI | None = None,
        *,
        model: str | None = None,
        dimensions: int | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client or OpenAI(api_key=self.settings.openai_api_key or None)
        self.model = model or self.settings.openai_embedding_model
        self.dimensions = dimensions or self.settings.openai_embedding_dims

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed every text, preserving input order."""
        response = self._client.embeddings.create(
            model=self.model,
            input=list(texts),
            dimensions=self.dimensions,
        )
        ordered = sorted(response.data, key=lambda item: item.index)
        return [list(item.embedding) for item in ordered]