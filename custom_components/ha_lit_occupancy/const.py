"""Constantes pour l'intégration Occupation du lit (ha_lit_occupancy)."""

DOMAIN = "ha_lit_occupancy"
STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = f"{DOMAIN}_state"

CONF_SOURCE_SENSOR = "source_sensor"
CONF_BASELINE = "baseline"
CONF_TOLERANCE = "tolerance_pct"
CONF_STABILITY = "stability_seconds"
CONF_LEARNING_ENABLED = "learning_enabled"
CONF_PERSONS = "persons"
CONF_ADD_ANOTHER = "add_another"

CONF_NAME = "name"
CONF_WEIGHT = "weight"

DEFAULT_BASELINE = 0.0
DEFAULT_TOLERANCE = 15  # en pourcentage du poids attendu
DEFAULT_STABILITY = 4  # secondes de stabilité avant de valider un changement
DEFAULT_EMPTY_TOLERANCE = 5.0  # tolérance absolue (kg) pour le cas "lit vide"
DEFAULT_LEARNING_ENABLED = True

# Paramètres d'auto-apprentissage du poids
LEARNING_SETTLE_SECONDS = 20.0  # durée stable suppl. avant d'affiner le poids d'une personne
LEARNING_ALPHA_FAST = 0.25  # vitesse d'apprentissage initiale (< 10 échantillons)
LEARNING_ALPHA_SLOW = 0.08  # vitesse d'apprentissage en régime établi
LEARNING_FAST_SAMPLES = 10  # nombre d'échantillons avant de passer en apprentissage doux
LEARNING_ACCESSORY_OFFSET_KG = 3.5  # marge haute (habillé, PC portable, livre...)
LEARNING_MAX_DRIFT_RATIO = 0.25  # écart max (25%) autorisé entre poids initial et poids appris

MIN_PERSONS = 1
MAX_PERSONS = 6
