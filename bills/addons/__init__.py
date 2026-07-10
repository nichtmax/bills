"""Addon registry. Add new providers here."""

from __future__ import annotations

from ..core.addon import Addon
from .cursor import CursorAddon
from .proton import ProtonAddon
from .vodafone import VodafoneAddon
from .zai import ZaiAddon

REGISTRY: dict[str, type[Addon]] = {
    VodafoneAddon.name: VodafoneAddon,
    CursorAddon.name: CursorAddon,
    ProtonAddon.name: ProtonAddon,
    ZaiAddon.name: ZaiAddon,
}


def get_addon(name: str) -> type[Addon]:
    key = name.strip().lower()
    if key not in REGISTRY:
        raise KeyError(f"unknown addon '{name}'. Known: {', '.join(sorted(REGISTRY))}")
    return REGISTRY[key]
