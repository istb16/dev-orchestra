"""AI Development Orchestrator -- supporting library for the Agent Skill.

The skill itself is prompt-driven; this package holds only the parts that
benefit from being deterministic: configuration, provider adapters, and the
mechanical half of the review pipeline.
"""

from __future__ import annotations

__all__ = ["__version__"]
__version__ = "0.1.0"
