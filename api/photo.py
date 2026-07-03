"""The tiny photo container shared by the provider seams (vision + depth).

Lives in its own neutral module so `api/vision_provider.py` and
`api/depth_provider.py` can both accept the same type without one having to
import the other. Re-exported from `api/vision_provider` for backward
compatibility with existing imports.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PhotoInput:
    content: bytes
    mime_type: str = "image/jpeg"
