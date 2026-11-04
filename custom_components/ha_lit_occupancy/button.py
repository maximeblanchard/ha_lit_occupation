"""Boutons d'action pour Occupation du lit (ha_lit_occupancy)."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LitOccupancyHub
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Configure les boutons de recalibration et de réinitialisation."""
    hub: LitOccupancyHub = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            LitRecalibrateButton(hub, entry),
            LitResetLearningButton(hub, entry),
        ]
    )


class LitRecalibrateButton(ButtonEntity):
    """Force la baseline à la valeur actuelle en déclarant le lit vide."""

    _attr_should_poll = False
    _attr_icon = "mdi:target-variant"

    def __init__(self, hub: LitOccupancyHub, entry: ConfigEntry) -> None:
        self._hub = hub
        self._entry = entry
        self._attr_name = "Recalibrer (lit vide maintenant)"
        self._attr_unique_id = f"{entry.entry_id}_recalibrate"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._entry.entry_id)})

    async def async_press(self) -> None:
        self._hub.async_force_empty_and_recalibrate()


class LitResetLearningButton(ButtonEntity):
    """Réinitialise les poids appris aux poids initiaux configurés."""

    _attr_should_poll = False
    _attr_icon = "mdi:restore"

    def __init__(self, hub: LitOccupancyHub, entry: ConfigEntry) -> None:
        self._hub = hub
        self._entry = entry
        self._attr_name = "Réinitialiser les poids appris"
        self._attr_unique_id = f"{entry.entry_id}_reset_learning"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._entry.entry_id)})

    async def async_press(self) -> None:
        self._hub.async_reset_learned_weights()
