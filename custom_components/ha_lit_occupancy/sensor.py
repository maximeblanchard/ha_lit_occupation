"""Entités sensor (poids estimé, poids appris et compteurs journaliers de temps)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfMass, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import LitOccupancyHub
from .const import CONF_NAME, CONF_PERSONS, DOMAIN


def _format_duration(seconds: float) -> str:
    """Formate une durée en secondes sous forme lisible 'Xh Ymin' ou 'Ymin'."""
    total_sec = max(0, int(round(seconds)))
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes:02d}min"
    return f"{minutes}min"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Configure les capteurs de poids et les compteurs de temps / sommeil."""
    hub: LitOccupancyHub = hass.data[DOMAIN][entry.entry_id]
    config = {**entry.data, **entry.options}

    entities: list[SensorEntity] = [LitEstimatedWeightSensor(hub, entry)]
    for person in config.get(CONF_PERSONS, []):
        name = person[CONF_NAME]
        entities.append(LitPersonLearnedWeightSensor(hub, entry, name))
        entities.append(LitPersonLastNightSleepSensor(hub, entry, name))
        entities.append(LitPersonInBedTimeSensor(hub, entry, name))
        entities.append(LitPersonOutOfBedTimeSensor(hub, entry, name))

    async_add_entities(entities)


class _LitBaseSensor(SensorEntity):
    """Base commune pour les capteurs numériques de poids du lit."""

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


class _LitBaseTimeSensor(SensorEntity):
    """Base commune pour les capteurs temporels (au lit, hors du lit, dernière nuit)."""

    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_suggested_display_precision = 1

    def __init__(self, hub: LitOccupancyHub, entry: ConfigEntry, name: str) -> None:
        self._hub = hub
        self._entry = entry
        self._person_name = name
        self._last_written_rounded: float | None = None
        self._last_written_ts: float = 0.0
        self._last_written_in_bed: bool | None = None

    async def async_added_to_hass(self) -> None:
        self._hub.async_add_time_listener(self._handle_update)

    def _handle_update(self, force: bool = False) -> None:
        """Met à jour l'état Home Assistant avec throttling pour éviter de saturer la base de données."""
        now_ts = dt_util.now().timestamp()
        is_in_bed = self._person_name in self._hub.current_present
        val = self.native_value
        rounded_val = round(val, 1) if val is not None else None

        # On écrit l'état si :
        # 1. Mise à jour forcée (ex: transition de présence, rollover minuit, fin de nuit)
        # 2. Changement du statut de présence au lit
        # 3. La valeur affichée à l'arrondi (0.1h = ~6 min) a bougé
        # 4. Au moins 5 minutes se sont écoulées en régime stable
        if (
            force
            or is_in_bed != self._last_written_in_bed
            or rounded_val != self._last_written_rounded
            or (now_ts - self._last_written_ts) >= 300.0
        ):
            self._last_written_rounded = rounded_val
            self._last_written_in_bed = is_in_bed
            self._last_written_ts = now_ts
            self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.title or "Occupation du lit",
            manufacturer="Occupation du lit",
            model="Détection adaptative par delta de poids",
        )


class LitPersonLastNightSleepSensor(_LitBaseTimeSensor):
    """Expose la durée de la dernière nuit complète (ou en cours) de sommeil par personne."""

    _attr_icon = "mdi:bed"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hub: LitOccupancyHub, entry: ConfigEntry, name: str) -> None:
        super().__init__(hub, entry, name)
        self._attr_name = f"Dernière nuit {name}"
        self._attr_unique_id = f"{entry.entry_id}_{name}_last_night_sleep"

    @property
    def native_value(self) -> float:
        stats = self._hub.person_daily_stats.get(self._person_name, {})
        is_in_bed = self._person_name in self._hub.current_present
        is_active = stats.get("sleep_session_active", False)

        # Si la personne est actuellement au lit dans une session de nuit
        if is_active and is_in_bed:
            start_ts = stats.get("sleep_start_ts")
            if start_ts:
                current_ts = dt_util.now().timestamp()
                interr = float(stats.get("sleep_interruption_seconds", 0.0))
                elapsed = max(0.0, current_ts - start_ts - interr)
                return round(elapsed / 3600.0, 2)

        # Sinon, retourne la durée de la dernière nuit complète validée
        sec = float(stats.get("sleep_duration_seconds", 0.0))
        return round(sec / 3600.0, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        stats = self._hub.person_daily_stats.get(self._person_name, {})
        is_in_bed = self._person_name in self._hub.current_present
        is_active = stats.get("sleep_session_active", False)

        if is_active and is_in_bed:
            start_ts = stats.get("sleep_start_ts")
            current_ts = dt_util.now().timestamp()
            interr = float(stats.get("sleep_interruption_seconds", 0.0))
            sec = max(0.0, current_ts - start_ts - interr) if start_ts else 0.0
            bed_time = stats.get("sleep_start_iso")
            wake_time = None
        else:
            sec = float(stats.get("sleep_duration_seconds", 0.0))
            bed_time = stats.get("last_night_bed_iso") or stats.get("sleep_start_iso")
            wake_time = stats.get("last_night_wake_iso") or stats.get("sleep_wake_iso")

        interr_sec = float(stats.get("sleep_interruption_seconds", 0.0))
        nap_sec = float(stats.get("last_nap_seconds", 0.0))

        attrs: dict[str, Any] = {
            "temps_formate": _format_duration(sec),
            "total_secondes": int(round(sec)),
            "total_minutes": round(sec / 60.0, 1),
            "total_heures": round(sec / 3600.0, 2),
            "heure_coucher": bed_time,
            "heure_lever": wake_time,
            "en_cours": bool(is_active and is_in_bed),
            "interruptions": int(stats.get("sleep_interruptions", 0)),
            "temps_interruptions": _format_duration(interr_sec),
        }
        if nap_sec > 0:
            attrs["derniere_sieste_formatee"] = _format_duration(nap_sec)
            attrs["derniere_sieste_debut"] = stats.get("last_nap_start_iso")
            attrs["derniere_sieste_fin"] = stats.get("last_nap_end_iso")

        return attrs


class LitPersonInBedTimeSensor(_LitBaseTimeSensor):
    """Compteur journalier du temps cumulé passé au lit par personne."""

    _attr_icon = "mdi:bed-clock"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, hub: LitOccupancyHub, entry: ConfigEntry, name: str) -> None:
        super().__init__(hub, entry, name)
        self._attr_name = f"Temps au lit {name}"
        self._attr_unique_id = f"{entry.entry_id}_{name}_time_in_bed"

    @property
    def native_value(self) -> float:
        stats = self._hub.person_daily_stats.get(self._person_name)
        if not stats:
            return 0.0
        sec = float(stats.get("in_bed_seconds", 0.0))
        return round(sec / 3600.0, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        stats = self._hub.person_daily_stats.get(self._person_name, {})
        sec = float(stats.get("in_bed_seconds", 0.0))
        yesterday_sec = float(stats.get("yesterday_in_bed_seconds", 0.0))
        night_sec = float(stats.get("sleep_duration_seconds", 0.0))
        is_in_bed = self._person_name in self._hub.current_present
        return {
            "temps_formate": _format_duration(sec),
            "total_secondes": int(round(sec)),
            "total_minutes": round(sec / 60.0, 1),
            "total_heures": round(sec / 3600.0, 2),
            "temps_hier_formate": _format_duration(yesterday_sec),
            "secondes_hier": int(round(yesterday_sec)),
            "au_lit_actuellement": is_in_bed,
            "coucher_derniere_nuit": (
                stats.get("last_night_bed_iso") or stats.get("sleep_start_iso")
            ),
            "lever_derniere_nuit": (
                stats.get("last_night_wake_iso") or stats.get("sleep_wake_iso")
            ),
            "duree_derniere_nuit": _format_duration(night_sec),
            "derniere_entree_lit": stats.get("last_in_bed"),
            "derniere_sortie_lit": stats.get("last_out_bed"),
            "date": stats.get("date"),
        }


class LitPersonOutOfBedTimeSensor(_LitBaseTimeSensor):
    """Compteur journalier du temps passé hors du lit par personne."""

    _attr_icon = "mdi:walk"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, hub: LitOccupancyHub, entry: ConfigEntry, name: str) -> None:
        super().__init__(hub, entry, name)
        self._attr_name = f"Temps hors du lit {name}"
        self._attr_unique_id = f"{entry.entry_id}_{name}_time_out_of_bed"

    @property
    def native_value(self) -> float:
        stats = self._hub.person_daily_stats.get(self._person_name)
        if not stats:
            return 0.0
        sec = float(stats.get("out_of_bed_seconds", 0.0))
        return round(sec / 3600.0, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        stats = self._hub.person_daily_stats.get(self._person_name, {})
        sec = float(stats.get("out_of_bed_seconds", 0.0))
        yesterday_sec = float(stats.get("yesterday_out_bed_seconds", 0.0))
        is_in_bed = self._person_name in self._hub.current_present

        # Calcul du temps continu depuis le dernier lever
        continuous_out_sec = 0.0
        if not is_in_bed:
            out_start = stats.get("continuous_out_of_bed_start_ts")
            if out_start:
                continuous_out_sec = max(0.0, dt_util.now().timestamp() - out_start)
            elif stats.get("last_out_bed"):
                parsed = dt_util.parse_datetime(stats["last_out_bed"])
                if parsed:
                    continuous_out_sec = max(
                        0.0, dt_util.now().timestamp() - parsed.timestamp()
                    )

        return {
            "temps_formate": _format_duration(sec),
            "temps_depuis_lever_formate": (
                _format_duration(continuous_out_sec)
                if not is_in_bed
                else "Actuellement au lit"
            ),
            "duree_continue_hors_du_lit_heures": (
                round(continuous_out_sec / 3600.0, 2) if not is_in_bed else 0.0
            ),
            "total_secondes": int(round(sec)),
            "total_minutes": round(sec / 60.0, 1),
            "total_heures": round(sec / 3600.0, 2),
            "temps_hier_formate": _format_duration(yesterday_sec),
            "secondes_hier": int(round(yesterday_sec)),
            "au_lit_actuellement": is_in_bed,
            "dernier_lever": stats.get("last_out_bed"),
            "date": stats.get("date"),
        }
