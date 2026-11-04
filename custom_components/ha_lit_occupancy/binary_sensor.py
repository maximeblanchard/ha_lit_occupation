"""Entités binary_sensor pour Occupation du lit (ha_lit_occupancy)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LitOccupancyHub
from .const import CONF_NAME, CONF_PERSONS, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Configure les capteurs binaires d'occupation du lit."""
    hub: LitOccupancyHub = hass.data[DOMAIN][entry.entry_id]
    config = {**entry.data, **entry.options}

    entities: list[BinarySensorEntity] = []
    for person in config.get(CONF_PERSONS, []):
        entities.append(LitPersonBinarySensor(hub, entry, person[CONF_NAME]))
    entities.append(LitAnyoneBinarySensor(hub, entry))

    async_add_entities(entities)


class _LitBaseBinarySensor(BinarySensorEntity):
    """Base commune : s'abonne au hub pour se mettre à jour."""

    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(self, hub: LitOccupancyHub, entry: ConfigEntry) -> None:
        self._hub = hub
        self._entry = entry

    async def async_added_to_hass(self) -> None:
        self._hub.async_add_listener(self._handle_update)

    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title or "Occupation du lit",
            manufacturer="Occupation du lit",
            model="Détection adaptative par delta de poids",
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "poids_brut": self._hub.last_raw,
            "baseline": round(self._hub.baseline, 2),
            "delta": (
                None
                if self._hub.last_raw is None
                else round(self._hub.last_raw - self._hub.baseline, 2)
            ),
            "confiance_elevee": self._hub.last_confident,
            "personnes_detectees": sorted(self._hub.current_present),
            "derniere_calibration": self._hub.last_calibrated,
        }


class LitPersonBinarySensor(_LitBaseBinarySensor):
    """Une entité par personne : est-elle sur le lit ?"""

    def __init__(self, hub: LitOccupancyHub, entry: ConfigEntry, name: str) -> None:
        super().__init__(hub, entry)
        self._person_name = name
        self._attr_name = f"{name} sur le lit"
        self._attr_unique_id = f"{entry.entry_id}_{name}"

    @property
    def is_on(self) -> bool:
        return self._person_name in self._hub.current_present

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(super().extra_state_attributes)
        stats = self._hub.person_stats.get(self._person_name)
        if stats:
            attrs["poids_initial_kg"] = round(float(stats["initial_weight"]), 2)
            attrs["poids_appris_kg"] = round(float(stats["learned_weight"]), 2)
            attrs["echantillons_apprentissage"] = int(stats["sample_count"])
            attrs["ecart_moyen_kg"] = round(float(stats["avg_deviation"]), 2)
        return attrs


class LitAnyoneBinarySensor(_LitBaseBinarySensor):
    """Entité globale : y a-t-il quelqu'un sur le lit (utile pour automations)."""

    def __init__(self, hub: LitOccupancyHub, entry: ConfigEntry) -> None:
        super().__init__(hub, entry)
        self._attr_name = "Lit occupé"
        self._attr_unique_id = f"{entry.entry_id}_occupied"

    @property
    def is_on(self) -> bool:
        return len(self._hub.current_present) > 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(super().extra_state_attributes)
        attrs["poids_appris_personnes"] = {
            name: round(float(stats["learned_weight"]), 2)
            for name, stats in self._hub.person_stats.items()
        }
        return attrs
