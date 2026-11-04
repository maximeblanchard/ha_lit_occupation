"""Entités sensor (poids réel estimé sur le lit et poids appris par personne)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfMass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LitOccupancyHub
from .const import CONF_NAME, CONF_PERSONS, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Configure les capteurs de poids estimé et de poids appris."""
    hub: LitOccupancyHub = hass.data[DOMAIN][entry.entry_id]
    config = {**entry.data, **entry.options}

    entities: list[SensorEntity] = [LitEstimatedWeightSensor(hub, entry)]
    for person in config.get(CONF_PERSONS, []):
        entities.append(LitPersonLearnedWeightSensor(hub, entry, person[CONF_NAME]))

    async_add_entities(entities)


class _LitBaseSensor(SensorEntity):
    """Base commune pour les capteurs numériques du lit."""

    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfMass.KILOGRAMS

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


class LitEstimatedWeightSensor(_LitBaseSensor):
    """Estime le vrai poids présent sur le lit (>= 0 kg) malgré les tares/reboots de l'ESP."""

    _attr_icon = "mdi:scale"
    _attr_suggested_display_precision = 1

    def __init__(self, hub: LitOccupancyHub, entry: ConfigEntry) -> None:
        super().__init__(hub, entry)
        self._attr_name = "Poids estimé sur le lit"
        self._attr_unique_id = f"{entry.entry_id}_estimated_weight"

    @property
    def native_value(self) -> float | None:
        if self._hub.last_raw is None:
            return None
        # Soustrait la baseline (qui absorbe les tares à 0 kg et les dérives)
        # et borne à 0.0 kg minimum pour éviter les valeurs négatives transitoires
        # pendant les quelques secondes de stabilisation à la sortie du lit.
        delta = self._hub.last_raw - self._hub.baseline
        return round(max(0.0, delta), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        delta_raw = (
            None
            if self._hub.last_raw is None
            else round(self._hub.last_raw - self._hub.baseline, 2)
        )
        return {
            "poids_brut_capteur_kg": self._hub.last_raw,
            "baseline_kg": round(self._hub.baseline, 2),
            "delta_brut_kg": delta_raw,
            "personnes_detectees": sorted(self._hub.current_present),
            "confiance_elevee": self._hub.last_confident,
            "derniere_calibration": self._hub.last_calibrated,
        }


class LitPersonLearnedWeightSensor(_LitBaseSensor):
    """Expose le poids appris pour une personne afin de suivre son évolution dans HA."""

    _attr_icon = "mdi:scale-bathroom"
    _attr_suggested_display_precision = 1

    def __init__(self, hub: LitOccupancyHub, entry: ConfigEntry, name: str) -> None:
        super().__init__(hub, entry)
        self._person_name = name
        self._attr_name = f"Poids appris {name}"
        self._attr_unique_id = f"{entry.entry_id}_{name}_learned_weight"

    @property
    def native_value(self) -> float | None:
        stats = self._hub.person_stats.get(self._person_name)
        if not stats:
            return None
        return round(float(stats["learned_weight"]), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        stats = self._hub.person_stats.get(self._person_name)
        if not stats:
            return {}
        return {
            "poids_initial_kg": round(float(stats["initial_weight"]), 2),
            "echantillons_apprentissage": int(stats["sample_count"]),
            "ecart_moyen_kg": round(float(stats["avg_deviation"]), 2),
            "apprentissage_actif": self._hub.learning_enabled,
        }
