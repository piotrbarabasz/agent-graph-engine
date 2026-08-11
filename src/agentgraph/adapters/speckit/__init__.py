"""Read-only concrete adapter for the supported source compatibility contract."""

from .adapter import SpecKitAdapter
from .paths import SpecKitLayout

__all__ = ["SpecKitAdapter", "SpecKitLayout"]
