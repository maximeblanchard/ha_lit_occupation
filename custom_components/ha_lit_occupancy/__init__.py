"""Intégration Occupation du lit (ha_lit_occupancy).

Transforme un capteur de poids brut (sujet à dérive du zéro ou à une tare à 0 kg
lors d'un redémarrage alors que le lit est occupé) en une détection fiable de
"qui est sur le lit", en combinant :
- l'analyse des paliers absolus (delta par rapport à la baseline),
- la détection des fronts montants et descendants (variations brusques entre deux paliers),
- l'auto-calibration bidirectionnelle de la baseline (lit vide),
- l'auto-apprentissage progressif du poids réel de chaque occupant.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import itertools
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BASELINE,
    CONF_LEARNING_ENABLED,
    CONF_NAME,
    CONF_PERSONS,
    CONF_SOURCE_SENSOR,
    CONF_STABILITY,
    CONF_TOLERANCE,
    CONF_WEIGHT,
    DEFAULT_BASELINE,
    DEFAULT_EMPTY_TOLERANCE,
    DEFAULT_LEARNING_ENABLED,
    DEFAULT_STABILITY,
    DEFAULT_TOLERANCE,
    DOMAIN,
    LEARNING_ACCESSORY_OFFSET_KG,
    LEARNING_ALPHA_FAST,
    LEARNING_ALPHA_SLOW,
    LEARNING_FAST_SAMPLES,
    LEARNING_MAX_DRIFT_RATIO,
    LEARNING_SETTLE_SECONDS,
    MIN_SIGNIFICANT_STEP_KG,
    MULTI_PERSON_COUPLING,
    NIGHT_INTERRUPTION_MAX_SECONDS,
    NIGHT_MIN_DURATION_SECONDS,
    PERIODIC_UPDATE_INTERVAL_SECONDS,
    STEP_MOTION_THRESHOLD_KG,
    STORAGE_KEY_PREFIX,
    STORAGE_SCHEMA_VERSION,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["binary_sensor", "button", "sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Configure une entrée Occupation du lit."""
    hub = LitOccupancyHub(hass, entry)
    await hub.async_load_storage()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    hub.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Décharge une entrée Occupation du lit."""
    hub: LitOccupancyHub = hass.data[DOMAIN][entry.entry_id]
    await hub.async_save_storage()
    hub.async_stop()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Recharge l'intégration lorsque ses options sont modifiées."""
    await hass.config_entries.async_reload(entry.entry_id)


class LitOccupancyHub:
    """Gère le suivi du poids, la détection de fronts, l'auto-calibration et l'apprentissage."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        config = {**entry.data, **entry.options}

        self.source_sensor: str = config[CONF_SOURCE_SENSOR]
        self.baseline: float = float(config.get(CONF_BASELINE, DEFAULT_BASELINE))
        self.tolerance_pct: float = float(config.get(CONF_TOLERANCE, DEFAULT_TOLERANCE))
        self.stability_seconds: float = float(
            config.get(CONF_STABILITY, DEFAULT_STABILITY)
        )
        self.learning_enabled: bool = bool(
            config.get(CONF_LEARNING_ENABLED, DEFAULT_LEARNING_ENABLED)
        )
        self.persons: list[dict[str, Any]] = [
            dict(p) for p in config.get(CONF_PERSONS, [])
        ]

        # Statistiques d'apprentissage par personne :
        # {nom: {"initial_weight": float, "learned_weight": float, "sample_count": int, "avg_deviation": float}}
        self.person_stats: dict[str, dict[str, Any]] = {}
        for p in self.persons:
            name = p[CONF_NAME]
            init_w = float(p[CONF_WEIGHT])
            self.person_stats[name] = {
                "initial_weight": init_w,
                "learned_weight": init_w,
                "sample_count": 0,
                "avg_deviation": 0.0,
            }

        # Statistiques journalières de temps au lit et hors du lit par personne
        # {nom: {"date": "YYYY-MM-DD", "in_bed_seconds": float, "out_of_bed_seconds": float, ...}}
        self.person_daily_stats: dict[str, dict[str, Any]] = {}
        for p in self.persons:
            name = p[CONF_NAME]
            self.person_daily_stats[name] = self._make_default_daily_stats()

        # État courant confirmé (après stabilisation) : set des noms présents
        self.current_present: set[str] = set()
        # Dernière valeur brute connue du capteur, et dernier palier stable validé
        self.last_raw: float | None = None
        self.last_stable_raw: float | None = None
        self.last_confident: bool = True
        self.last_calibrated: Any = None
        self._baseline_verified: bool = False

        # Suivi d'indisponibilité pour détecter un vrai reboot de l'ESP à 0.0 kg
        self._source_was_unavailable: bool = False
        self._raw_before_unavailable: float | None = None

        self._unsub_state: Any = None
        self._unsub_timer: Any = None
        self._unsub_learn_timer: Any = None
        self._unsub_interval: Any = None
        self._pending_present: set[str] | None = None
        self._learning_samples: list[float] = []
        self._listeners: list[Any] = []
        self._time_listeners: list[Any] = []
        self._last_storage_save: float = dt_util.now().timestamp()

        self._store = Store[dict[str, Any]](
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}_{entry.entry_id}"
        )

        self._combinations: list[tuple[frozenset[str], float]] = []
        self._rebuild_combinations()

    def _rebuild_combinations(self) -> None:
        """Précalcule toutes les combinaisons possibles avec les poids appris.

        Lorsqu'au moins 2 personnes sont ensemble sur le lit, on applique un léger
        facteur de couplage (MULTI_PERSON_COUPLING = 0.92) car la répartition sur
        le sommier/cadre réduit légèrement la somme brute mesurée par rapport à la
        somme des poids individuels mesurés séparément.
        """
        self._combinations = []
        names = [p[CONF_NAME] for p in self.persons]
        weights = {name: self.get_effective_weight(name) for name in names}
        for r in range(0, len(names) + 1):
            for combo in itertools.combinations(names, r):
                raw_sum = sum(weights[n] for n in combo)
                if r >= 2:
                    expected = raw_sum * MULTI_PERSON_COUPLING
                else:
                    expected = raw_sum
                self._combinations.append((frozenset(combo), expected))

    def get_effective_weight(self, name: str) -> float:
        """Retourne le poids appris (ou initial si l'apprentissage est désactivé)."""
        stats = self.person_stats.get(name)
        if not stats:
            return 0.0
        if not self.learning_enabled:
            return float(stats["initial_weight"])
        return float(stats["learned_weight"])

    def _make_default_daily_stats(self) -> dict[str, Any]:
        """Génère la structure par défaut des compteurs journaliers et session de nuit pour une personne."""
        local_now = dt_util.as_local(dt_util.now())
        today_str = local_now.date().isoformat()
        return {
            "date": today_str,
            "in_bed_seconds": 0.0,
            "out_of_bed_seconds": 0.0,
            "last_update": local_now.timestamp(),
            "last_in_bed": None,
            "last_out_bed": None,
            "yesterday_in_bed_seconds": 0.0,
            "yesterday_out_bed_seconds": 0.0,
            # Métriques de session de sommeil / nuit (indépendantes du reset de minuit)
            "sleep_session_active": False,
            "sleep_start_ts": None,
            "sleep_start_iso": None,
            "sleep_wake_ts": None,
            "sleep_wake_iso": None,
            "sleep_duration_seconds": 0.0,
            "sleep_interruptions": 0,
            "sleep_interruption_seconds": 0.0,
            "pending_exit_ts": None,
            "continuous_out_of_bed_start_ts": None,
            "last_night_bed_iso": None,
            "last_night_wake_iso": None,
            "last_nap_seconds": 0.0,
            "last_nap_start_iso": None,
            "last_nap_end_iso": None,
        }

    def _update_time_trackers(self, now: Any | None = None) -> tuple[bool, bool]:
        """Met à jour les compteurs journaliers et les sessions de sommeil pour chaque personne.

        Prend en compte le passage à minuit en heure locale (rollover) pour archiver la veille
        et réinitialiser le compteur du nouveau jour.
        Valide également la clôture d'une nuit de sommeil après un lever continu > 15 min.
        Retourne (changed, day_rollover).
        """
        current_dt = dt_util.as_local(now if now is not None else dt_util.now())
        current_ts = current_dt.timestamp()
        current_date_str = current_dt.date().isoformat()
        changed = False
        day_rollover = False

        for p in self.persons:
            name = p[CONF_NAME]
            stats = self.person_daily_stats.setdefault(
                name, self._make_default_daily_stats()
            )
            last_ts = float(stats.get("last_update", current_ts))
            elapsed = max(0.0, current_ts - last_ts)
            stats["last_update"] = current_ts

            is_in_bed = name in self.current_present

            # 1. Clôture de la session de sommeil après un lever définitif (> 15 minutes)
            pending_exit = stats.get("pending_exit_ts")
            if pending_exit is not None and not is_in_bed:
                if (current_ts - pending_exit) >= NIGHT_INTERRUPTION_MAX_SECONDS:
                    wake_dt = dt_util.as_local(
                        datetime.fromtimestamp(pending_exit, tz=timezone.utc)
                    )
                    stats["sleep_wake_ts"] = pending_exit
                    stats["sleep_wake_iso"] = wake_dt.isoformat()

                    sleep_start = stats.get("sleep_start_ts")
                    if sleep_start is not None:
                        interr_sec = float(stats.get("sleep_interruption_seconds", 0.0))
                        raw_duration = max(0.0, pending_exit - sleep_start - interr_sec)

                        if raw_duration >= NIGHT_MIN_DURATION_SECONDS:
                            start_dt = dt_util.as_local(
                                datetime.fromtimestamp(sleep_start, tz=timezone.utc)
                            )
                            # Une nuit commence le soir/nuit (>= 19h ou < 7h) OU dure au moins 3h
                            is_night_session = (
                                start_dt.hour >= 19
                                or start_dt.hour < 7
                                or raw_duration >= 10800.0
                            )
                            if is_night_session:
                                stats["sleep_duration_seconds"] = round(raw_duration, 1)
                                stats["last_night_bed_iso"] = stats.get("sleep_start_iso")
                                stats["last_night_wake_iso"] = stats["sleep_wake_iso"]
                            else:
                                stats["last_nap_seconds"] = round(raw_duration, 1)
                                stats["last_nap_start_iso"] = stats.get("sleep_start_iso")
                                stats["last_nap_end_iso"] = stats["sleep_wake_iso"]

                    stats["sleep_session_active"] = False
                    stats["pending_exit_ts"] = None
                    stats["continuous_out_of_bed_start_ts"] = pending_exit
                    changed = True

            if elapsed <= 0.0:
                continue

            changed = True
            saved_date = stats.get("date", current_date_str)

            if saved_date == current_date_str:
                if is_in_bed:
                    stats["in_bed_seconds"] = stats.get("in_bed_seconds", 0.0) + elapsed
                else:
                    stats["out_of_bed_seconds"] = (
                        stats.get("out_of_bed_seconds", 0.0) + elapsed
                    )
            else:
                day_rollover = True
                # Minuit en HEURE LOCALE a été franchi entre last_ts et current_ts
                midnight_today = current_dt.replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                midnight_ts = midnight_today.timestamp()

                elapsed_yesterday = max(0.0, min(elapsed, midnight_ts - last_ts))
                elapsed_today = max(0.0, current_ts - max(last_ts, midnight_ts))

                if is_in_bed:
                    yesterday_total = (
                        stats.get("in_bed_seconds", 0.0) + elapsed_yesterday
                    )
                    stats["yesterday_in_bed_seconds"] = round(yesterday_total, 1)
                    stats["yesterday_out_bed_seconds"] = round(
                        stats.get("out_of_bed_seconds", 0.0), 1
                    )
                    stats["in_bed_seconds"] = elapsed_today
                    stats["out_of_bed_seconds"] = 0.0
                else:
                    yesterday_total = (
                        stats.get("out_of_bed_seconds", 0.0) + elapsed_yesterday
                    )
                    stats["yesterday_out_bed_seconds"] = round(yesterday_total, 1)
                    stats["yesterday_in_bed_seconds"] = round(
                        stats.get("in_bed_seconds", 0.0), 1
                    )
                    stats["in_bed_seconds"] = 0.0
                    stats["out_of_bed_seconds"] = elapsed_today

                stats["date"] = current_date_str

        return changed, day_rollover

    @callback
    def _async_periodic_tracker_update(self, now: Any) -> None:
        """Appelé périodiquement toutes les 60 secondes pour rafraîchir les capteurs de temps."""
        changed, day_rollover = self._update_time_trackers(now)
        if changed:
            self._notify_time_listeners(force=day_rollover)
            current_ts = dt_util.now().timestamp()
            if day_rollover or (current_ts - self._last_storage_save > 900):
                self._last_storage_save = current_ts
                self._schedule_save()

    async def async_load_storage(self) -> None:
        """Charge la baseline et les poids appris depuis le stockage persistant."""
        stored = await self._store.async_load()
        if not stored:
            return

        schema_version = stored.get("schema_version", 1)
        if schema_version < STORAGE_SCHEMA_VERSION:
            _LOGGER.info(
                "Migration du stockage vers le schéma %d : purge de l'ancienne baseline potentiellement corrompue.",
                STORAGE_SCHEMA_VERSION,
            )
            config = {**self.entry.data, **self.entry.options}
            self.baseline = float(config.get(CONF_BASELINE, DEFAULT_BASELINE))
            self._baseline_verified = False
            self.current_present = set()
        else:
            if "baseline" in stored and stored["baseline"] is not None:
                self.baseline = float(stored["baseline"])
            if stored.get("last_calibrated"):
                self.last_calibrated = dt_util.parse_datetime(stored["last_calibrated"])
            if stored.get("baseline_verified"):
                self._baseline_verified = bool(stored["baseline_verified"])

            stored_present = stored.get("current_present")
            if isinstance(stored_present, list):
                valid_names = {p[CONF_NAME] for p in self.persons}
                self.current_present = {n for n in stored_present if n in valid_names}

        stored_stats = stored.get("person_stats", {})
        if isinstance(stored_stats, dict):
            for name, stats in self.person_stats.items():
                if name in stored_stats:
                    s = stored_stats[name]
                    saved_init = float(s.get("initial_weight", stats["initial_weight"]))
                    if abs(saved_init - stats["initial_weight"]) < 0.1:
                        stats["learned_weight"] = float(
                            s.get("learned_weight", stats["initial_weight"])
                        )
                        stats["sample_count"] = int(s.get("sample_count", 0))
                        stats["avg_deviation"] = float(s.get("avg_deviation", 0.0))

        # Restauration des compteurs journaliers et sessions de sommeil
        stored_daily = stored.get("person_daily_stats", {})
        if isinstance(stored_daily, dict):
            local_now = dt_util.as_local(dt_util.now())
            current_ts = local_now.timestamp()
            today_str = local_now.date().isoformat()
            yesterday_str = (local_now.date() - timedelta(days=1)).isoformat()
            midnight_today = local_now.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            midnight_ts = midnight_today.timestamp()

            for name, stats in self.person_daily_stats.items():
                if name in stored_daily and isinstance(stored_daily[name], dict):
                    sd = stored_daily[name]
                    saved_date = sd.get("date")

                    # Restauration complète des attributs et timestamps d'abord
                    for key in (
                        "last_in_bed",
                        "last_out_bed",
                        "sleep_session_active",
                        "sleep_start_ts",
                        "sleep_start_iso",
                        "sleep_wake_ts",
                        "sleep_wake_iso",
                        "sleep_duration_seconds",
                        "sleep_interruptions",
                        "sleep_interruption_seconds",
                        "pending_exit_ts",
                        "continuous_out_of_bed_start_ts",
                        "last_night_bed_iso",
                        "last_night_wake_iso",
                        "last_nap_seconds",
                        "last_nap_start_iso",
                        "last_nap_end_iso",
                    ):
                        if key in sd and sd[key] is not None:
                            stats[key] = sd[key]

                    in_bed_sec = float(sd.get("in_bed_seconds", 0.0))
                    out_bed_sec = float(sd.get("out_of_bed_seconds", 0.0))
                    yest_in_sec = float(sd.get("yesterday_in_bed_seconds", 0.0))
                    yest_out_sec = float(sd.get("yesterday_out_bed_seconds", 0.0))
                    last_saved_ts = float(sd.get("last_update", current_ts))

                    is_sleeping = bool(stats.get("sleep_session_active"))
                    was_in_bed = is_sleeping and stats.get("pending_exit_ts") is None

                    if saved_date == today_str:
                        # Si le stockage date d'aujourd'hui :
                        # On comble le temps éventuel passé pendant l'arrêt/redémarrage de Home Assistant
                        elapsed_down = max(0.0, current_ts - last_saved_ts)
                        if was_in_bed:
                            # Si la personne dormait avant minuit et dort toujours aujourd'hui :
                            # Le temps passé au lit aujourd'hui (depuis 00h00) ne peut pas être inférieur au temps écoulé depuis minuit !
                            sleep_start = float(stats.get("sleep_start_ts") or current_ts)
                            interr = float(stats.get("sleep_interruption_seconds", 0.0))
                            if sleep_start < midnight_ts:
                                elapsed_since_midnight = max(0.0, current_ts - midnight_ts - interr)
                                in_bed_sec = max(in_bed_sec + elapsed_down, elapsed_since_midnight)
                            else:
                                in_bed_sec += elapsed_down
                            out_bed_sec = 0.0
                        else:
                            out_bed_sec += elapsed_down

                        stats["in_bed_seconds"] = in_bed_sec
                        stats["out_of_bed_seconds"] = out_bed_sec
                        stats["yesterday_in_bed_seconds"] = yest_in_sec
                        stats["yesterday_out_bed_seconds"] = yest_out_sec

                    elif saved_date == yesterday_str:
                        # Si le stockage datait d'hier avant minuit :
                        # Minuit a été franchi pendant le redémarrage ou avant la sauvegarde !
                        elapsed_to_midnight = max(0.0, midnight_ts - last_saved_ts)
                        elapsed_today = max(0.0, current_ts - midnight_ts)

                        if was_in_bed:
                            stats["yesterday_in_bed_seconds"] = in_bed_sec + elapsed_to_midnight
                            stats["yesterday_out_bed_seconds"] = out_bed_sec
                            # Pour aujourd'hui (depuis 00h00) : tout le temps écoulé depuis minuit a été passé au lit
                            stats["in_bed_seconds"] = elapsed_today
                            stats["out_of_bed_seconds"] = 0.0
                        else:
                            stats["yesterday_out_bed_seconds"] = out_bed_sec + elapsed_to_midnight
                            stats["yesterday_in_bed_seconds"] = in_bed_sec
                            stats["in_bed_seconds"] = 0.0
                            stats["out_of_bed_seconds"] = elapsed_today

                    stats["date"] = today_str
                    stats["last_update"] = current_ts

        self._rebuild_combinations()

    async def async_save_storage(self) -> None:
        """Sauvegarde la baseline, les poids appris et compteurs de temps sur disque."""
        data = {
            "schema_version": STORAGE_SCHEMA_VERSION,
            "baseline": round(self.baseline, 3),
            "baseline_verified": self._baseline_verified,
            "last_calibrated": (
                self.last_calibrated.isoformat()
                if hasattr(self.last_calibrated, "isoformat")
                else self.last_calibrated
            ),
            "current_present": sorted(self.current_present),
            "person_stats": self.person_stats,
            "person_daily_stats": self.person_daily_stats,
        }
        await self._store.async_save(data)

    def _schedule_save(self) -> None:
        """Planifie une sauvegarde asynchrone sans bloquer la boucle d'événements."""
        self.hass.async_create_task(self.async_save_storage())

    def async_add_listener(self, callback_fn: Any) -> None:
        """Permet aux entités (poids, présence) de s'abonner aux changements d'état."""
        self._listeners.append(callback_fn)

    def async_add_time_listener(self, callback_fn: Any) -> None:
        """Permet aux entités temporelles (durées, sommeil) de s'abonner aux mises à jour."""
        self._time_listeners.append(callback_fn)

    def _notify_listeners(self, force_time: bool = False) -> None:
        """Notifie toutes les entités de l'intégration."""
        for listener in self._listeners:
            listener()
        for listener in self._time_listeners:
            listener(force_time)

    def _notify_time_listeners(self, force: bool = False) -> None:
        """Notifie uniquement les entités de temps et sommeil (sans réécrire les capteurs de poids)."""
        for listener in self._time_listeners:
            listener(force)

    @callback
    def async_start(self) -> None:
        self._unsub_state = async_track_state_change_event(
            self.hass, [self.source_sensor], self._async_source_changed
        )
        self._unsub_interval = async_track_time_interval(
            self.hass,
            self._async_periodic_tracker_update,
            timedelta(seconds=PERIODIC_UPDATE_INTERVAL_SECONDS),
        )
        state = self.hass.states.get(self.source_sensor)
        if state is not None:
            self._handle_new_value(state)

    @callback
    def async_stop(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_interval:
            self._unsub_interval()
            self._unsub_interval = None
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
        if self._unsub_learn_timer:
            self._unsub_learn_timer()
            self._unsub_learn_timer = None

    @callback
    def _async_source_changed(self, event: Event) -> None:
        new_state: State | None = event.data.get("new_state")
        if new_state is None:
            return
        self._handle_new_value(new_state)

    def _handle_new_value(self, state: State) -> None:
        try:
            raw = float(state.state)
        except (ValueError, TypeError):
            # Capteur "unknown"/"unavailable" : on mémorise le dernier poids stable
            # avant la coupure pour distinguer une micro-coupure Wi-Fi d'un vrai reboot ESP.
            if not self._source_was_unavailable:
                self._source_was_unavailable = True
                self._raw_before_unavailable = self.last_stable_raw or self.last_raw
            return

        prev_raw = self.last_raw
        self.last_raw = raw

        # Est-on en plein front montant ou descendant (variation rapide entre 2 mesures) ?
        in_motion = (
            prev_raw is not None and abs(raw - prev_raw) > STEP_MOTION_THRESHOLD_KG
        )

        # Si le capteur est en plein mouvement (front montant ou descendant en cours),
        # on ne valide RIEN tout de suite : on relance le timer de stabilité pour
        # attendre que le palier d'arrivée soit atteint et stable.
        if in_motion:
            candidate, confident = self._evaluate_candidate(raw)
            self.last_confident = confident
            self._pending_present = candidate
            self._start_stability_timer()
            self._notify_listeners()
            return

        candidate, confident = self._evaluate_candidate(raw)
        self.last_confident = confident

        # Collecte d'échantillons si une seule personne est stable sur le lit
        if (
            self.learning_enabled
            and self._baseline_verified
            and candidate == self.current_present
            and len(self.current_present) == 1
            and confident
        ):
            delta = raw - self.baseline
            if delta > DEFAULT_EMPTY_TOLERANCE:
                self._learning_samples.append(delta)

        # Vérifie si on a besoin de recalibrer la baseline alors que le lit est déjà vide
        needs_baseline_update = (
            not candidate
            and not self.current_present
            and abs(raw - self.baseline) > 1.5
        )

        if candidate == self.current_present and not needs_baseline_update:
            # L'état correspond déjà à l'état confirmé : on annule un éventuel
            # timer de changement d'état en attente.
            if self._unsub_timer:
                self._unsub_timer()
                self._unsub_timer = None
                self._pending_present = None
            if self.last_stable_raw is None:
                self.last_stable_raw = raw
            self._notify_listeners()
            return

        # Si la même combinaison candidate est déjà en cours de stabilisation et que
        # le capteur n'est pas en mouvement brusque, on laisse le timer aller au bout.
        if self._pending_present == candidate and self._unsub_timer is not None:
            self._notify_listeners()
            return

        self._pending_present = candidate
        self._start_stability_timer()
        if self.stability_seconds > 0:
            self._notify_listeners()

    def _start_stability_timer(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None

        if self.stability_seconds <= 0:
            self._commit_pending()
        else:
            self._unsub_timer = async_call_later(
                self.hass, self.stability_seconds, self._async_confirm_pending
            )

    @callback
    def _async_confirm_pending(self, _now: Any) -> None:
        self._unsub_timer = None
        self._commit_pending()

    def _commit_pending(self) -> None:
        if self.last_raw is None:
            return

        raw = self.last_raw

        # 1. Vérification d'un VRAI reboot de l'ESP avec tare à 0.0 kg :
        # Uniquement si :
        # - le capteur vient de passer par unavailable/unknown,
        # - le nouveau palier STABLE est revenu pile autour de 0.0 kg (|raw| <= 2.5 kg),
        # - avant la coupure la valeur brute était nettement éloignée de 0 (|raw_before| > 15 kg),
        # - et la variation par rapport à avant la coupure est importante (> 15 kg), ce qui
        #   exclut une simple micro-coupure Wi-Fi où la valeur reste identique (ex: +3.3 -> unknown -> +3.3).
        if (
            self._source_was_unavailable
            and abs(raw) <= 2.5
            and self._raw_before_unavailable is not None
            and abs(self._raw_before_unavailable) > 15.0
            and abs(self._raw_before_unavailable - raw) > 15.0
        ):
            if not self.current_present:
                # Le lit était vide avant le reboot : l'ESP s'est retaré à 0.0 kg pour le lit vide.
                _LOGGER.info(
                    "Reboot de l'ESP détecté à %.2f kg alors que le lit était vide (avant coupure: %.2f kg) : "
                    "recalage automatique de la baseline à %.2f kg",
                    raw,
                    self._raw_before_unavailable,
                    raw,
                )
                self.async_recalibrate(raw)
                self._baseline_verified = True
                self._source_was_unavailable = False
                self._raw_before_unavailable = None
                self.last_stable_raw = raw
                self._pending_present = None
                self.current_present = set()
                self._schedule_save()
                self._notify_listeners()
                return

            occupants_weight = self._expected_combo_weight(self.current_present)
            if occupants_weight > DEFAULT_EMPTY_TOLERANCE:
                _LOGGER.info(
                    "Vrai reboot de l'ESP détecté à %.2f kg (avant coupure: %.2f kg) avec %s : "
                    "ajustement de la baseline à %.2f kg",
                    raw,
                    self._raw_before_unavailable,
                    sorted(self.current_present),
                    raw - occupants_weight,
                )
                self.async_recalibrate(raw - occupants_weight)
                self._source_was_unavailable = False
                self._raw_before_unavailable = None
                self.last_stable_raw = raw
                self._pending_present = None
                self._schedule_save()
                self._notify_listeners()
                return

        self._source_was_unavailable = False
        self._raw_before_unavailable = None

        # 2. Réévalue le candidat sur la valeur stabilisée finale (en tenant compte du front)
        candidate, confident = self._evaluate_candidate(raw)
        self.last_confident = confident
        previous_present = set(self.current_present)
        previous_stable_raw = self.last_stable_raw

        # Met à jour les compteurs de temps sur l'état précédent avant la transition
        self._update_time_trackers()

        self.current_present = candidate
        self._pending_present = None

        # Enregistre l'horodatage de la dernière entrée ou sortie pour chaque personne
        local_now = dt_util.as_local(dt_util.now())
        now_iso = local_now.isoformat()
        current_ts = local_now.timestamp()

        for name in self.person_daily_stats:
            stats = self.person_daily_stats[name]
            entered = name in self.current_present and name not in previous_present
            exited = name not in self.current_present and name in previous_present

            if entered:
                stats["last_in_bed"] = now_iso
                stats["continuous_out_of_bed_start_ts"] = None

                pending_exit = stats.get("pending_exit_ts")
                if (
                    stats.get("sleep_session_active")
                    and pending_exit is not None
                    and (current_ts - pending_exit) <= NIGHT_INTERRUPTION_MAX_SECONDS
                ):
                    # Retour au lit après un lever bref nocturne (toilettes, verre d'eau...)
                    interr_duration = current_ts - pending_exit
                    stats["sleep_interruptions"] = (
                        int(stats.get("sleep_interruptions", 0)) + 1
                    )
                    stats["sleep_interruption_seconds"] = (
                        float(stats.get("sleep_interruption_seconds", 0.0))
                        + interr_duration
                    )
                    stats["pending_exit_ts"] = None
                elif (
                    stats.get("sleep_session_active")
                    and stats.get("sleep_start_ts") is not None
                ):
                    # Session déjà active (ex: redémarrage de Home Assistant, rechargement ou retour de détection)
                    # On préserve absolument l'heure de coucher initiale !
                    stats["pending_exit_ts"] = None
                else:
                    # Nouvelle session au lit
                    stats["pending_exit_ts"] = None
                    stats["sleep_session_active"] = True
                    stats["sleep_start_ts"] = current_ts
                    stats["sleep_start_iso"] = now_iso
                    stats["sleep_wake_ts"] = None
                    stats["sleep_wake_iso"] = None
                    stats["sleep_interruptions"] = 0
                    stats["sleep_interruption_seconds"] = 0.0

            elif exited:
                stats["last_out_bed"] = now_iso
                stats["continuous_out_of_bed_start_ts"] = current_ts
                if stats.get("sleep_session_active"):
                    stats["pending_exit_ts"] = current_ts

        if self._unsub_learn_timer:
            self._unsub_learn_timer()
            self._unsub_learn_timer = None
        self._learning_samples.clear()

        # 3. Auto-calibration de la baseline dès que l'état stable confirmé est "lit vide" !
        # Que la valeur brute soit plus basse que l'ancienne baseline (ex: -80 kg après un
        # démarrage à 0 kg) OU plus haute qu'une baseline qui avait dérivé trop bas
        # (ex: -80 kg après une dérive à -137 kg suite à un front descendant complet),
        # le palier stable "lit vide" EST la nouvelle référence physique du lit vide.
        if not self.current_present and confident:
            step_drop = (
                (previous_stable_raw - raw)
                if previous_stable_raw is not None
                else (self.baseline - raw)
            )
            if (
                self.learning_enabled
                and len(previous_present) == 1
                and step_drop > MIN_SIGNIFICANT_STEP_KG
            ):
                self._try_learn_from_step(step_drop, previous_present)

            if abs(raw - self.baseline) > 0.05 or not self._baseline_verified:
                _LOGGER.info(
                    "État 'lit vide' stable confirmé à %.2f kg (ancienne baseline: %.2f kg) : "
                    "calibration automatique de la baseline.",
                    raw,
                    self.baseline,
                )
                self.async_recalibrate(raw)
            self._baseline_verified = True

        # 4. Auto-correction si la baseline était manifestement trop haute alors que tout le
        # monde est dans le lit, ou trop basse (delta >> poids total de tous les occupants) :
        elif self.current_present and len(self.current_present) == len(self.persons):
            max_expected = self._expected_combo_weight(self.current_present)
            delta = raw - self.baseline
            # Si le delta dépasse de plus de 25 kg le poids de TOUS les occupants réunis,
            # c'est que la baseline avait glissé anormalement bas : on la recale.
            if max_expected > 0 and delta > max_expected + 25.0:
                corrected_baseline = raw - max_expected
                _LOGGER.warning(
                    "Delta anormalement élevé (%.1f kg > max attendu %.1f kg) : "
                    "correction automatique de la baseline de %.2f kg à %.2f kg",
                    delta,
                    max_expected,
                    self.baseline,
                    corrected_baseline,
                )
                self.async_recalibrate(corrected_baseline)

        self.last_stable_raw = raw

        # 5. Si une seule personne est confirmée sur le lit avec une baseline vérifiée,
        # on lance la fenêtre d'apprentissage de 20 secondes.
        if (
            self.learning_enabled
            and self._baseline_verified
            and len(self.current_present) == 1
            and self.last_confident
        ):
            delta = raw - self.baseline
            if delta > DEFAULT_EMPTY_TOLERANCE:
                self._learning_samples = [delta]
                self._unsub_learn_timer = async_call_later(
                    self.hass,
                    LEARNING_SETTLE_SECONDS,
                    self._async_learn_settled_weight,
                )

        self._schedule_save()
        self._notify_listeners(force_time=True)

    def _expected_combo_weight(self, combo: set[str] | frozenset[str]) -> float:
        """Calcule le poids attendu d'un ensemble de personnes (avec couplage si >= 2)."""
        raw_sum = sum(self.get_effective_weight(name) for name in combo)
        if len(combo) >= 2:
            return raw_sum * MULTI_PERSON_COUPLING
        return raw_sum

    def _evaluate_candidate(self, raw: float) -> tuple[set[str], bool]:
        """Détermine qui est sur le lit en combinant l'analyse des fronts et le delta absolu.

        Pourquoi combiner les deux ?
        1. Si le poids descend nettement sous la baseline (ex: 0 kg -> -80 kg), le lit est vide.
        2. Si on connaît l'état stable précédent (self.last_stable_raw et self.current_present)
           et qu'un front franc (montant ou descendant >= 25 kg) vient de se produire :
           - Un front descendant dont l'amplitude correspond au poids de TOUS les occupants
             présents signifie que le lit vient de se vider complètement (même si la baseline
             était décalée !).
           - Un front descendant partiel (ex: -66 kg quand Maxime + Nhung étaient dans le lit)
             indique qu'au moins une personne est sortie : on retire la personne dont le poids
             correspond au front descendant.
           - Un front montant franc (ex: +45 kg ou +67 kg) quand 1 personne était déjà dans le
             lit indique qu'une autre personne vient de monter sur le lit.
        """
        delta = raw - self.baseline

        # 1. Plancher sous la baseline : lit vide certain (ex: sortie du lit vers -80 kg)
        if delta <= DEFAULT_EMPTY_TOLERANCE:
            return set(), True

        # 2. Analyse par front (step change) par rapport au dernier palier stable
        if self.last_stable_raw is not None:
            step = raw - self.last_stable_raw

            # --- FRONT DESCENDANT FRANC (quelqu'un sort du lit) ---
            if step <= -MIN_SIGNIFICANT_STEP_KG and self.current_present:
                drop = -step  # valeur positive de la perte de poids
                present_expected = self._expected_combo_weight(self.current_present)

                # 2a. La perte de poids correspond-elle au départ de TOUS les occupants présents ?
                # (ex: 1 personne de 54 kg présente et chute de 58 kg, ou 2 personnes = 128 kg et chute de 128 kg)
                if present_expected > 0 and drop >= present_expected * 0.72:
                    # Vérifie s'il ne reste pas quand même une personne clairement identifiable
                    # lorsque 2 personnes étaient présentes et que seule la plus lourde est sortie :
                    if len(self.current_present) >= 2:
                        # Compare la chute (drop) avec le départ d'une seule personne vs tout le monde
                        best_subset_rem, subset_err = self._best_remaining_after_drop(
                            drop, raw
                        )
                        all_left_err = (
                            abs(drop - present_expected) * 0.65
                            + abs(raw - self.baseline) * 0.35
                        )
                        if best_subset_rem is not None and subset_err < all_left_err:
                            return best_subset_rem, True
                    return set(), True

                # 2b. Départ partiel (ex: 2 personnes étaient sur le lit et 1 seule sort)
                if len(self.current_present) >= 2:
                    best_rem, _ = self._best_remaining_after_drop(drop, raw)
                    if best_rem is not None:
                        return best_rem, True

            # --- FRONT MONTANT FRANC (quelqu'un monte sur le lit) ---
            elif step >= MIN_SIGNIFICANT_STEP_KG:
                gain = step
                # Si des personnes étaient déjà présentes et qu'un front montant >= 30 kg
                # survient, au moins une personne supplémentaire est montée sur le lit.
                if self.current_present and len(self.current_present) < len(self.persons):
                    best_added = self._best_added_after_gain(gain, raw)
                    if best_added is not None:
                        return best_added, True

        # 3. Classement par plus proche voisin sur le delta absolu
        return self._best_match(raw)

    def _best_remaining_after_drop(
        self, drop: float, raw: float
    ) -> tuple[set[str] | None, float]:
        """Trouve quel sous-ensemble strict d'occupants reste sur le lit après une chute de poids."""
        present_list = list(self.current_present)
        total_before = self._expected_combo_weight(self.current_present)
        delta_after = raw - self.baseline

        best_remaining: set[str] | None = None
        best_score = float("inf")

        # Teste tous les sous-ensembles non vides strictement plus petits que current_present
        for r in range(1, len(present_list)):
            for rem_tuple in itertools.combinations(present_list, r):
                rem_set = set(rem_tuple)
                rem_expected = self._expected_combo_weight(rem_set)
                expected_drop = total_before - rem_expected
                # Combine l'erreur sur l'amplitude du saut et (si la baseline est saine) l'erreur sur le palier restant
                drop_err = abs(drop - expected_drop)
                level_err = abs(delta_after - rem_expected)
                score = drop_err * 0.65 + level_err * 0.35
                if score < best_score:
                    best_score = score
                    best_remaining = rem_set

        return best_remaining, best_score

    def _best_added_after_gain(self, gain: float, raw: float) -> set[str] | None:
        """Trouve quel sur-ensemble d'occupants est sur le lit après un front montant."""
        all_names = {p[CONF_NAME] for p in self.persons}
        absent_list = list(all_names - self.current_present)
        if not absent_list:
            return None

        total_before = self._expected_combo_weight(self.current_present)
        delta_after = raw - self.baseline

        best_new_set: set[str] | None = None
        best_score = float("inf")

        for r in range(1, len(absent_list) + 1):
            for added_tuple in itertools.combinations(absent_list, r):
                new_set = self.current_present | set(added_tuple)
                new_expected = self._expected_combo_weight(new_set)
                expected_gain = new_expected - total_before
                gain_err = abs(gain - expected_gain)
                level_err = abs(delta_after - new_expected)
                score = gain_err * 0.65 + level_err * 0.35
                if score < best_score and gain >= expected_gain * 0.60:
                    best_score = score
                    best_new_set = new_set

        return best_new_set

    def _try_learn_from_step(
        self, step_weight: float, previous_present: set[str]
    ) -> None:
        """Apprend à partir d'une sortie franche d'une personne seule vers le lit vide."""
        if len(previous_present) != 1:
            return
        candidate_name = next(iter(previous_present))
        self._update_person_learned_weight(candidate_name, step_weight)

    @callback
    def _async_learn_settled_weight(self, _now: Any) -> None:
        """Affine le poids de la personne seule dans le lit après un palier stable."""
        self._unsub_learn_timer = None
        if (
            not self.learning_enabled
            or not self._baseline_verified
            or len(self.current_present) != 1
            or not self.last_confident
            or not self._learning_samples
        ):
            return

        person_name = next(iter(self.current_present))
        sorted_samples = sorted(self._learning_samples)
        mid = len(sorted_samples) // 2
        if len(sorted_samples) % 2 == 1:
            observed_weight = sorted_samples[mid]
        else:
            observed_weight = (sorted_samples[mid - 1] + sorted_samples[mid]) / 2.0

        self._learning_samples.clear()
        if self._update_person_learned_weight(person_name, observed_weight):
            self._schedule_save()
            self._notify_listeners()

    def _update_person_learned_weight(
        self, name: str, observed_weight: float
    ) -> bool:
        """Met à jour le poids appris d'une personne avec protection contre les accessoires."""
        stats = self.person_stats.get(name)
        if not stats:
            return False

        init_w = float(stats["initial_weight"])
        current_w = float(stats["learned_weight"])
        count = int(stats["sample_count"])
        avg_dev = float(stats["avg_deviation"])

        # Garde-fou global : on ignore une mesure trop éloignée du poids initial configuré
        max_drift = max(init_w * LEARNING_MAX_DRIFT_RATIO, 15.0)
        if abs(observed_weight - init_w) > max_drift:
            return False

        diff = observed_weight - current_w
        abs_diff = abs(diff)

        alpha = (
            LEARNING_ALPHA_FAST
            if count < LEARNING_FAST_SAMPLES
            else LEARNING_ALPHA_SLOW
        )

        # Si le poids mesuré dépasse de plus de 1.5 kg le poids appris habituel (count >= 3),
        # on réduit l'impact pour ne pas intégrer le poids d'un PC ou de vêtements lourds.
        if count >= 3 and diff > 1.5:
            alpha *= 0.25

        new_w = current_w + alpha * diff
        new_w = max(init_w - max_drift, min(init_w + max_drift, new_w))

        new_dev = (
            abs_diff
            if count == 0
            else (1.0 - LEARNING_ALPHA_SLOW) * avg_dev + LEARNING_ALPHA_SLOW * abs_diff
        )

        stats["learned_weight"] = round(new_w, 2)
        stats["sample_count"] = count + 1
        stats["avg_deviation"] = round(new_dev, 2)

        self._rebuild_combinations()
        _LOGGER.debug(
            "Apprentissage poids pour %s : observé=%.2f kg -> appris=%.2f kg (échantillons=%d, écart moyen=±%.2f kg)",
            name,
            observed_weight,
            stats["learned_weight"],
            stats["sample_count"],
            stats["avg_deviation"],
        )
        return True

    @callback
    def async_recalibrate(self, raw_value: float | None = None) -> None:
        """Force la baseline à la valeur brute donnée (ou la dernière connue)."""
        value = raw_value if raw_value is not None else self.last_raw
        if value is None:
            return
        self.baseline = value
        self.last_calibrated = dt_util.utcnow()

    @callback
    def async_force_empty_and_recalibrate(self) -> None:
        """Utilisé par le bouton manuel : déclare le lit vide immédiatement et recalibre."""
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
        if self._unsub_learn_timer:
            self._unsub_learn_timer()
            self._unsub_learn_timer = None
        self._pending_present = None
        self._learning_samples.clear()
        self.current_present = set()
        self.last_confident = True
        self._baseline_verified = True
        if self.last_raw is not None:
            self.async_recalibrate(self.last_raw)
            self.last_stable_raw = self.last_raw
        self._schedule_save()
        self._notify_listeners(force_time=True)

    @callback
    def async_reset_learned_weights(self) -> None:
        """Réinitialise les poids appris aux valeurs initiales configurées."""
        for stats in self.person_stats.values():
            stats["learned_weight"] = float(stats["initial_weight"])
            stats["sample_count"] = 0
            stats["avg_deviation"] = 0.0
        self._rebuild_combinations()
        self._schedule_save()
        self._notify_listeners(force_time=True)

    def _best_match(self, raw: float) -> tuple[set[str], bool]:
        """Trouve la combinaison de personnes la plus proche du delta observé."""
        delta = raw - self.baseline

        if delta <= DEFAULT_EMPTY_TOLERANCE:
            return set(), True

        best_combo: frozenset[str] = frozenset()
        best_distance = float("inf")
        best_tol = DEFAULT_EMPTY_TOLERANCE

        for combo, expected in self._combinations:
            if expected == 0:
                distance = abs(delta)
                tol = DEFAULT_EMPTY_TOLERANCE
            else:
                accessory_allowance = LEARNING_ACCESSORY_OFFSET_KG * len(combo)
                if delta >= expected:
                    effective_diff = max(
                        0.0, (delta - expected) - accessory_allowance * 0.5
                    )
                else:
                    effective_diff = expected - delta
                distance = effective_diff
                tol = expected * (self.tolerance_pct / 100.0) + accessory_allowance

            if distance < best_distance:
                best_distance = distance
                best_combo = combo
                best_tol = tol

        confident = best_distance <= best_tol
        return set(best_combo), confident
