"""Intégration Occupation du lit (ha_lit_occupancy).

Transforme un capteur de poids brut (sujet à dérive du zéro ou à une tare à 0 kg
lors d'un redémarrage alors que le lit est occupé) en une détection fiable de
"qui est sur le lit", avec auto-calibration de la baseline et auto-apprentissage
du poids réel de chaque occupant.
"""
from __future__ import annotations

import itertools
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
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
    STORAGE_KEY_PREFIX,
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
    """Gère le suivi du poids, l'auto-calibration et l'apprentissage des poids."""

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

        # État courant confirmé (après stabilisation) : set des noms présents
        self.current_present: set[str] = set()
        # Dernière valeur brute connue du capteur, et confiance du dernier calcul
        self.last_raw: float | None = None
        self.last_stable_raw: float | None = None
        self.last_confident: bool = True
        self.last_calibrated: Any = None

        # Suivi d'indisponibilité pour détecter un reboot de l'ESP à 0 kg
        self._source_was_unavailable: bool = False

        self._unsub_state: Any = None
        self._unsub_timer: Any = None
        self._unsub_learn_timer: Any = None
        self._pending_present: set[str] | None = None
        self._learning_samples: list[float] = []
        self._listeners: list[Any] = []

        self._store = Store[dict[str, Any]](
            hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}_{entry.entry_id}"
        )

        self._combinations: list[tuple[frozenset[str], float]] = []
        self._rebuild_combinations()

    def _rebuild_combinations(self) -> None:
        """Précalcule toutes les combinaisons possibles avec les poids appris."""
        self._combinations = []
        names = [p[CONF_NAME] for p in self.persons]
        weights = {
            name: self.get_effective_weight(name) for name in names
        }
        for r in range(0, len(names) + 1):
            for combo in itertools.combinations(names, r):
                total = sum(weights[n] for n in combo)
                self._combinations.append((frozenset(combo), total))

    def get_effective_weight(self, name: str) -> float:
        """Retourne le poids appris (ou initial si l'apprentissage est désactivé)."""
        stats = self.person_stats.get(name)
        if not stats:
            return 0.0
        if not self.learning_enabled:
            return float(stats["initial_weight"])
        return float(stats["learned_weight"])

    async def async_load_storage(self) -> None:
        """Charge la baseline et les poids appris depuis le stockage persistant."""
        stored = await self._store.async_load()
        if not stored:
            return

        if "baseline" in stored and stored["baseline"] is not None:
            self.baseline = float(stored["baseline"])
        if stored.get("last_calibrated"):
            self.last_calibrated = dt_util.parse_datetime(stored["last_calibrated"])

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
                    # Si l'utilisateur n'a pas changé manuellement le poids initial dans la config,
                    # on restaure le poids appris. Sinon on repart du nouveau poids configuré.
                    if abs(saved_init - stats["initial_weight"]) < 0.1:
                        stats["learned_weight"] = float(
                            s.get("learned_weight", stats["initial_weight"])
                        )
                        stats["sample_count"] = int(s.get("sample_count", 0))
                        stats["avg_deviation"] = float(s.get("avg_deviation", 0.0))

        self._rebuild_combinations()

    async def async_save_storage(self) -> None:
        """Sauvegarde la baseline et les poids appris sur disque."""
        data = {
            "baseline": round(self.baseline, 3),
            "last_calibrated": (
                self.last_calibrated.isoformat()
                if hasattr(self.last_calibrated, "isoformat")
                else self.last_calibrated
            ),
            "current_present": sorted(self.current_present),
            "person_stats": self.person_stats,
        }
        await self._store.async_save(data)

    def _schedule_save(self) -> None:
        """Planifie une sauvegarde asynchrone sans bloquer la boucle d'événements."""
        self.hass.async_create_task(self.async_save_storage())

    def async_add_listener(self, callback_fn: Any) -> None:
        """Permet aux entités de s'abonner aux mises à jour."""
        self._listeners.append(callback_fn)

    def _notify_listeners(self) -> None:
        for listener in self._listeners:
            listener()

    @callback
    def async_start(self) -> None:
        self._unsub_state = async_track_state_change_event(
            self.hass, [self.source_sensor], self._async_source_changed
        )
        state = self.hass.states.get(self.source_sensor)
        if state is not None:
            self._handle_new_value(state)

    @callback
    def async_stop(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
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
            # Capteur "unknown"/"unavailable" : typique d'un reboot de l'ESP.
            self._source_was_unavailable = True
            return

        # Détection d'un reboot du capteur à 0 kg pendant que le lit était occupé :
        # Si le capteur revient d'un état indisponible (ou saute brutalement à ~0 kg)
        # alors que des personnes étaient confirmées sur le lit et que l'ancien poids
        # brut était éloigné de zéro, le capteur vient de refaire sa tare avec les
        # occupants dessus. On décale immédiatement la baseline de -(poids des occupants).
        if (
            self.current_present
            and abs(raw) <= DEFAULT_EMPTY_TOLERANCE
            and (
                self._source_was_unavailable
                or (
                    self.last_raw is not None
                    and abs(self.last_raw - raw) > DEFAULT_EMPTY_TOLERANCE * 3
                    and abs(self.baseline) > DEFAULT_EMPTY_TOLERANCE
                )
            )
        ):
            occupants_weight = sum(
                self.get_effective_weight(name) for name in self.current_present
            )
            if occupants_weight > DEFAULT_EMPTY_TOLERANCE:
                _LOGGER.info(
                    "Redémarrage probable du capteur détecté à %.2f kg avec %s sur le lit : "
                    "ajustement automatique de la baseline à %.2f kg",
                    raw,
                    sorted(self.current_present),
                    raw - occupants_weight,
                )
                self.async_recalibrate(raw - occupants_weight)

        self._source_was_unavailable = False
        self.last_raw = raw

        candidate, confident = self._best_match(raw)
        self.last_confident = confident

        # Collecte d'échantillons si une seule personne est stable sur le lit
        if (
            self.learning_enabled
            and candidate == self.current_present
            and len(self.current_present) == 1
            and confident
        ):
            delta = raw - self.baseline
            if delta > DEFAULT_EMPTY_TOLERANCE:
                self._learning_samples.append(delta)

        if candidate == self.current_present:
            # L'état correspond déjà à l'état confirmé : on annule un éventuel
            # timer de changement d'état en attente.
            if self._unsub_timer:
                self._unsub_timer()
                self._unsub_timer = None
                self._pending_present = None

            # Si le lit est déjà vide et que le poids brut descend encore plus bas
            # (ex: tare faite avec quelqu'un dans le lit, puis sortie du lit vers -80 kg)
            # ou dérive légèrement dans la zone vide, on maintient la baseline à jour.
            if not self.current_present and raw < self.baseline - DEFAULT_EMPTY_TOLERANCE:
                self._pending_present = set()
                self._start_stability_timer()
            self._notify_listeners()
            return

        # Si la même combinaison candidate est déjà en cours de stabilisation,
        # on NE réinitialise PAS le timer (sinon chaque micro-variation du capteur
        # repousserait indéfiniment la validation).
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
        if self._pending_present is None:
            return

        previous_present = set(self.current_present)
        previous_stable_raw = self.last_stable_raw

        self.current_present = self._pending_present
        self._pending_present = None

        if self._unsub_learn_timer:
            self._unsub_learn_timer()
            self._unsub_learn_timer = None
        self._learning_samples.clear()

        if self.last_raw is not None:
            # Cas 1 : Le poids brut est négatif par rapport à la baseline actuelle
            # (ex: capteur démarré à 0 kg alors que quelqu'un était dans le lit, puis
            # la personne sort du lit et le capteur affiche -80 kg).
            # Si on observe un saut vers le bas depuis le palier précédent, on peut même
            # utiliser la valeur du saut pour affiner le poids si une seule personne est sortie !
            if (
                not self.current_present
                and self.last_raw < self.baseline - DEFAULT_EMPTY_TOLERANCE
            ):
                step_drop = (
                    (previous_stable_raw if previous_stable_raw is not None else self.baseline)
                    - self.last_raw
                )
                if self.learning_enabled and step_drop > DEFAULT_EMPTY_TOLERANCE:
                    self._try_learn_from_step(step_drop, previous_present)

                _LOGGER.info(
                    "Poids stable inférieur à la baseline détecté (%.2f kg < %.2f kg) : "
                    "recalibration automatique du lit vide à %.2f kg",
                    self.last_raw,
                    self.baseline,
                    self.last_raw,
                )
                self.async_recalibrate(self.last_raw)
                self.last_confident = True

            # Cas 2 : Auto-calibration normale quand le lit redevient vide avec confiance
            elif not self.current_present and self.last_confident:
                self.async_recalibrate(self.last_raw)

            self.last_stable_raw = self.last_raw

        # Si une seule personne vient de s'installer dans le lit avec confiance,
        # on planifie une session d'auto-apprentissage après stabilisation complète.
        if (
            self.learning_enabled
            and len(self.current_present) == 1
            and self.last_confident
            and self.last_raw is not None
        ):
            delta = self.last_raw - self.baseline
            if delta > DEFAULT_EMPTY_TOLERANCE:
                self._learning_samples = [delta]
                self._unsub_learn_timer = async_call_later(
                    self.hass,
                    LEARNING_SETTLE_SECONDS,
                    self._async_learn_settled_weight,
                )

        self._schedule_save()
        self._notify_listeners()

    def _try_learn_from_step(
        self, step_weight: float, previous_present: set[str]
    ) -> None:
        """Apprend à partir d'une sortie franche du lit (ex: 0 kg -> -80 kg)."""
        # Si une seule personne était connue sur le lit, ou s'il n'y a qu'une personne
        # dont le poids correspond étroitement à ce saut
        candidate_name: str | None = None
        if len(previous_present) == 1:
            candidate_name = next(iter(previous_present))
        else:
            matching = []
            for p in self.persons:
                name = p[CONF_NAME]
                w = self.get_effective_weight(name)
                tol = w * (self.tolerance_pct / 100.0) + LEARNING_ACCESSORY_OFFSET_KG
                if abs(step_weight - w) <= tol:
                    matching.append(name)
            if len(matching) == 1:
                candidate_name = matching[0]

        if candidate_name:
            self._update_person_learned_weight(candidate_name, step_weight)

    @callback
    def _async_learn_settled_weight(self, _now: Any) -> None:
        """Affine le poids de la personne seule dans le lit après un palier stable."""
        self._unsub_learn_timer = None
        if (
            not self.learning_enabled
            or len(self.current_present) != 1
            or not self.last_confident
            or not self._learning_samples
        ):
            return

        person_name = next(iter(self.current_present))
        # On utilise la médiane des échantillons collectés pendant le repos
        # pour ignorer les pics liés aux mouvements dans le lit.
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
        """Met à jour le poids appris d'une personne avec protection contre les accessoires (PC, vêtements)."""
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

        # Choix de la vitesse d'apprentissage (rapide au début pour corriger le poids
        # approximatif saisi par l'utilisateur, puis lente ensuite).
        alpha = (
            LEARNING_ALPHA_FAST
            if count < LEARNING_FAST_SAMPLES
            else LEARNING_ALPHA_SLOW
        )

        # Gestion des variations "habillé + PC portable vs nu" :
        # Une fois les premières mesures effectuées (count >= 3), si le poids mesuré est
        # supérieur de plus de 1.5 kg au poids appris habituel, il est très probable que
        # la personne ait des vêtements lourds ou un objet (PC, livre). On réduit fortement
        # l'impact sur le poids de base pour ne pas décaler son poids corporel de référence.
        if count >= 3 and diff > 1.5:
            alpha *= 0.25

        new_w = current_w + alpha * diff
        # Bornage dans l'intervalle autorisé autour du poids initial
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
        if self.last_raw is not None:
            self.async_recalibrate(self.last_raw)
            self.last_stable_raw = self.last_raw
        self._schedule_save()
        self._notify_listeners()

    @callback
    def async_reset_learned_weights(self) -> None:
        """Réinitialise les poids appris aux valeurs initiales configurées."""
        for stats in self.person_stats.values():
            stats["learned_weight"] = float(stats["initial_weight"])
            stats["sample_count"] = 0
            stats["avg_deviation"] = 0.0
        self._rebuild_combinations()
        self._schedule_save()
        self._notify_listeners()

    def _best_match(self, raw: float) -> tuple[set[str], bool]:
        """Trouve la combinaison de personnes la plus proche du delta observé.

        Gère :
        - Les deltas négatifs (ex: -80 kg quand le capteur a démarré avec quelqu'un
          dans le lit puis que la personne est sortie) -> détecté comme lit vide
          avec confiance pour déclencher l'auto-recalibration sur ce nouveau plancher.
        - Les variations positives liées aux vêtements ou accessoires (PC portable, etc.).
        """
        delta = raw - self.baseline

        # Si le poids descend nettement sous la baseline (ex: -80 kg après un boot
        # capteur lit occupé), le lit s'est allégé sous sa référence actuelle :
        # c'est un état "lit vide" qui doit recalibrer la baseline vers le bas.
        if delta < -DEFAULT_EMPTY_TOLERANCE:
            return set(), True

        best_combo: frozenset[str] = frozenset()
        best_distance = float("inf")
        best_tol = DEFAULT_EMPTY_TOLERANCE

        for combo, expected in self._combinations:
            if expected == 0:
                distance = abs(delta)
                tol = DEFAULT_EMPTY_TOLERANCE
            else:
                # Tolérance asymétrique : on pardonne un léger excédent positif
                # (vêtements, ordinateur portable, livre) jusqu'à LEARNING_ACCESSORY_OFFSET_KG
                # par personne présente.
                accessory_allowance = LEARNING_ACCESSORY_OFFSET_KG * len(combo)
                if delta >= expected:
                    effective_diff = max(0.0, (delta - expected) - accessory_allowance * 0.5)
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
