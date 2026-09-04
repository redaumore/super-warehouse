"""Canonical deterministic command vocabulary for the owner chat.

Every command must arrive as its own message: the parsers behind these
commands use strict whole-message anchored matching, so a command buried
inside a longer sentence is never recognized. The vocabulary also includes
the session-reset trigger ("hola bob"), which follows the same anchored
whole-message contract: only a bare trigger message resets the conversation.
These constants are the single source of truth shared by the Customer
handler, the LLM system prompt, and the tests.
"""

import re

ADD_QUANTITY = "agregale N"
FINALIZE_BARE = "cerrá el pedido"
FINALIZE_WITH_CUSTOMER = "cerrá el pedido para <cliente>"
NEW_CUSTOMER = "nuevo cliente <nombre> <teléfono>"
APPROVE = "aprobá"
REJECT = "rechazá"
RESET_SESSION = "hola bob"
RESET_GREETING = (
    "¡Hola! Arrancamos de cero: decime el cliente, los productos y las cantidades."
)

_SESSION_RESET_RE = re.compile(r"^\s*hola\s+bob\s*[.!]*\s*$", re.IGNORECASE)


def is_session_reset(text: str) -> bool:
    """True when ``text`` is exactly the session-reset trigger, case-insensitive.

    Anchored whole-message match: only a bare "hola bob" (optionally with
    trailing dots or exclamation marks) resets the conversation. Empty text
    and longer sentences that merely contain the words are not a reset.
    """
    return _SESSION_RESET_RE.match((text or "").strip()) is not None
