"""Addon registry. Add new providers here."""

from __future__ import annotations

from ..core.addon import Addon
from .cursor import CursorAddon
from .vodafone import VodafoneAddon

REGISTRY: dict[str, type[Addon]] = {
    VodafoneAddon.name: VodafoneAddon,
    CursorAddon.name: CursorAddon,
}


def get_addon(name: str) -> type[Addon]:
    key = name.strip().lower()
    if key not in REGISTRY:
        raise KeyError(f"unknown addon '{name}'. Known: {', '.join(sorted(REGISTRY))}")
    return REGISTRY[key]
