"""WhatsApp Cloud API adapter tests (task 3.1).

The adapter implements the shared ``Channel`` contract: payload normalization
(text/voice/image/document), verify-token authentication, Graph API text sends
and media downloads. Every network call goes through ``httpx`` and is mocked;
no token, no network.

Settings are injected per test (``get_settings`` is lru-cached, so env-driven
values would leak across tests otherwise).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.channels.whatsapp import WhatsAppChannel, WhatsAppError
from src.config import Settings

_TEXT_PAYLOAD = {
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "messages": [
                            {
                                "from": "5491155551234",
                                "text": {"body": "clavos 2 pulgadas"},
                            }
                        ]
                    }
                }
            ]
        }
    ]
}

_VOICE_PAYLOAD = {
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "messages": [
                            {
                                "from": "5491155551234",
                                "voice": {"id": "media-1", "mime_type": "audio/ogg"},
                            }
                        ]
                    }
                }
            ]
        }
    ]
}

_IMAGE_PAYLOAD = {
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "messages": [
                            {
                                "from": "5491155551234",
                                "image": {"id": "media-2", "mime_type": "image/jpeg"},
                            }
                        ]
                    }
                }
            ]
        }
    ]
}

CONFIGURED = Settings(
    whatsapp_token="tok", whatsapp_phone_id="123456", whatsapp_verify_token="verifyme"
)
UNCONFIGURED = Settings(whatsapp_token="", whatsapp_phone_id="")


def _ok_response() -> MagicMock:
    response = MagicMock()
    response.raise_for_status.return_value = None
    return response


def _mock_client() -> AsyncMock:
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    return client


@pytest.mark.asyncio
async def test_parse_text_message_normalizes_sender_and_body():
    """Un mensaje de texto de WhatsApp se normaliza con remitente y cuerpo."""
    channel = WhatsAppChannel(settings=CONFIGURED)
    message = await channel.parse_inbound(_TEXT_PAYLOAD)
    assert message.channel == "whatsapp"
    assert message.sender_id == "5491155551234"
    assert message.text == "clavos 2 pulgadas"
    assert message.media_type is None


@pytest.mark.asyncio
async def test_parse_voice_message_flags_media_kind():
    """Una nota de voz se marca como media_type voice con su media_id."""
    channel = WhatsAppChannel(settings=CONFIGURED)
    message = await channel.parse_inbound(_VOICE_PAYLOAD)
    assert message.media_type == "voice"
    assert message.text is None
    assert message.raw["media_id"] == "media-1"
    assert message.raw["mime_type"] == "audio/ogg"


@pytest.mark.asyncio
async def test_parse_image_message_flags_media_kind():
    """Una foto se marca como media_type image con su media_id."""
    channel = WhatsAppChannel(settings=CONFIGURED)
    message = await channel.parse_inbound(_IMAGE_PAYLOAD)
    assert message.media_type == "image"
    assert message.raw["media_id"] == "media-2"


@pytest.mark.asyncio
async def test_parse_empty_payload_yields_empty_sender():
    """Un payload sin mensajes normaliza un InboundMessage vacío."""
    channel = WhatsAppChannel(settings=CONFIGURED)
    message = await channel.parse_inbound({})
    assert message.sender_id == ""
    assert message.text is None


def test_verify_request_checks_subscription_verify_token():
    """El token de suscripción del webhook autentica la verificación hub."""
    channel = WhatsAppChannel(settings=CONFIGURED)
    assert channel.verify_request({"hub.verify_token": "verifyme"}, None) is True
    assert channel.verify_request({"hub.verify_token": "wrong"}, None) is False


def test_verify_request_message_payload_defers_to_endpoint_hmac():
    """Un payload de mensaje confía en la firma HMAC ya validada por el endpoint."""
    channel = WhatsAppChannel(settings=CONFIGURED)
    assert channel.verify_request(_TEXT_PAYLOAD, "sha256=abc") is True
    assert channel.verify_request(_TEXT_PAYLOAD, None) is False


@pytest.mark.asyncio
async def test_send_text_posts_to_graph_messages_endpoint():
    """Enviar texto postea al endpoint de mensajes con el bearer token."""
    channel = WhatsAppChannel(settings=CONFIGURED)
    client = _mock_client()
    client.post.return_value = _ok_response()
    with patch("src.channels.whatsapp.httpx.AsyncClient", return_value=client):
        await channel.send_text("5491155551234", "hola")
    client.post.assert_awaited_once()
    url = client.post.await_args.args[0]
    assert url.endswith("/123456/messages")
    headers = client.post.await_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer tok"
    body = client.post.await_args.kwargs["json"]
    assert body["to"] == "5491155551234"
    assert body["text"]["body"] == "hola"


@pytest.mark.asyncio
async def test_send_text_without_config_is_noop():
    """Sin token ni phone id configurados, enviar texto es un no-op."""
    channel = WhatsAppChannel(settings=UNCONFIGURED)
    with patch("src.channels.whatsapp.httpx.AsyncClient") as client:
        await channel.send_text("5491155551234", "hola")
    client.assert_not_called()


@pytest.mark.asyncio
async def test_send_text_http_error_raises_whatsapp_error():
    """Un error HTTP al enviar se traduce en WhatsAppError."""
    channel = WhatsAppChannel(settings=CONFIGURED)
    client = _mock_client()
    response = MagicMock()
    response.raise_for_status.side_effect = httpx.HTTPError("boom")
    client.post.return_value = response
    with (
        patch("src.channels.whatsapp.httpx.AsyncClient", return_value=client),
        pytest.raises(WhatsAppError),
    ):
        await channel.send_text("5491155551234", "hola")


@pytest.mark.asyncio
async def test_fetch_media_resolves_id_to_bytes():
    """Descargar media resuelve el id a URL y devuelve los bytes."""
    channel = WhatsAppChannel(settings=CONFIGURED)
    client = _mock_client()
    meta = MagicMock()
    meta.raise_for_status.return_value = None
    meta.json.return_value = {"url": "https://cdn.whatsapp.net/abc"}
    data = MagicMock()
    data.raise_for_status.return_value = None
    data.content = b"raw-bytes"
    client.get.side_effect = [meta, data]
    with patch("src.channels.whatsapp.httpx.AsyncClient", return_value=client):
        result = await channel.fetch_media("media-9")
    assert result == b"raw-bytes"
    assert client.get.await_count == 2
    assert client.get.await_args_list[0].args[0].endswith("/media-9")


@pytest.mark.asyncio
async def test_fetch_media_without_token_raises_before_network():
    """Sin token configurado, descargar media falla sin tocar la red."""
    channel = WhatsAppChannel(settings=UNCONFIGURED)
    with patch("src.channels.whatsapp.httpx.AsyncClient") as client, pytest.raises(WhatsAppError):
        await channel.fetch_media("media-9")
    client.assert_not_called()
