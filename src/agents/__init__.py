"""Domain agents of the ferretería MVP.

Each agent owns the tools for one stage of the order pipeline: customer identity
resolution, catalog disambiguation, inventory soft-locking, perception
(STT/vision), conversational sales (quoting + adjustments) and dispatch (owner
notification + approve/reject). The orchestrator that coordinates them lives in
``src.orchestrator``.
"""
