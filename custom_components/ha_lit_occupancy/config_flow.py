"""Config flow pour Occupation du lit (ha_lit_occupancy)."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ADD_ANOTHER,
    CONF_BASELINE,
    CONF_LEARNING_ENABLED,
    CONF_NAME,
    CONF_PERSONS,
    CONF_SOURCE_SENSOR,
    CONF_STABILITY,
    CONF_TOLERANCE,
    CONF_WEIGHT,
    DEFAULT_BASELINE,
    DEFAULT_LEARNING_ENABLED,
    DEFAULT_STABILITY,
    DEFAULT_TOLERANCE,
    DOMAIN,
    MAX_PERSONS,
)


def _user_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_SOURCE_SENSOR, default=defaults.get(CONF_SOURCE_SENSOR)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Required(
                CONF_BASELINE, default=defaults.get(CONF_BASELINE, DEFAULT_BASELINE)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    mode=selector.NumberSelectorMode.BOX, step=0.1
                )
            ),
            vol.Required(
                CONF_TOLERANCE,
                default=defaults.get(CONF_TOLERANCE, DEFAULT_TOLERANCE),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    mode=selector.NumberSelectorMode.BOX,
                    min=1,
                    max=50,
                    step=1,
                    unit_of_measurement="%",
                )
            ),
            vol.Required(
                CONF_STABILITY,
                default=defaults.get(CONF_STABILITY, DEFAULT_STABILITY),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    mode=selector.NumberSelectorMode.BOX,
                    min=0,
                    max=60,
                    step=1,
                    unit_of_measurement="s",
                )
            ),
            vol.Required(
                CONF_LEARNING_ENABLED,
                default=defaults.get(CONF_LEARNING_ENABLED, DEFAULT_LEARNING_ENABLED),
            ): selector.BooleanSelector(),
        }
    )


def _person_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME): selector.TextSelector(),
            vol.Required(CONF_WEIGHT): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    mode=selector.NumberSelectorMode.BOX,
                    min=5.0,
                    max=300.0,
                    step=0.5,
                    unit_of_measurement="kg",
                )
            ),
            vol.Optional(CONF_ADD_ANOTHER, default=False): selector.BooleanSelector(),
        }
    )


class LitOccupancyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Gère le config flow initial pour Occupation du lit."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._persons: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_person()

        return self.async_show_form(
            step_id="user", data_schema=_user_schema(), errors=errors
        )

    async def async_step_person(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            existing_names = {p[CONF_NAME].lower() for p in self._persons}
            if not name:
                errors["base"] = "invalid_name"
            elif name.lower() in existing_names:
                errors["base"] = "duplicate_name"
            else:
                add_another = user_input.pop(CONF_ADD_ANOTHER, False)
                self._persons.append(
                    {
                        CONF_NAME: name,
                        CONF_WEIGHT: float(user_input[CONF_WEIGHT]),
                    }
                )
                if add_another and len(self._persons) < MAX_PERSONS:
                    return await self.async_step_person()

                self._data[CONF_PERSONS] = self._persons
                return self.async_create_entry(
                    title="Occupation du lit", data=self._data
                )

        return self.async_show_form(
            step_id="person",
            data_schema=_person_schema(),
            errors=errors,
            description_placeholders={"count": str(len(self._persons) + 1)},
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> LitOccupancyOptionsFlow:
        return LitOccupancyOptionsFlow(config_entry)


class LitOccupancyOptionsFlow(config_entries.OptionsFlow):
    """Permet de modifier la config (capteur, tolérance, personnes) après coup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        merged = {**config_entry.data, **config_entry.options}
        self._data: dict[str, Any] = dict(merged)
        self._persons: list[dict[str, Any]] = [
            dict(p) for p in self._data.get(CONF_PERSONS, [])
        ]
        self._persons_reset = False

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_persons_menu()

        return self.async_show_form(
            step_id="init", data_schema=_user_schema(self._data)
        )

    async def async_step_persons_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        return self.async_show_menu(
            step_id="persons_menu",
            menu_options=["keep_persons", "redo_persons"],
        )

    async def async_step_keep_persons(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        self._data[CONF_PERSONS] = self._persons
        self.hass.config_entries.async_update_entry(self._entry, data=self._data)
        return self.async_create_entry(title="", data=self._data)

    async def async_step_redo_persons(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if not self._persons_reset:
            self._persons = []
            self._persons_reset = True
        return await self.async_step_person()

    async def async_step_person(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            existing_names = {p[CONF_NAME].lower() for p in self._persons}
            if not name:
                errors["base"] = "invalid_name"
            elif name.lower() in existing_names:
                errors["base"] = "duplicate_name"
            else:
                add_another = user_input.pop(CONF_ADD_ANOTHER, False)
                self._persons.append(
                    {
                        CONF_NAME: name,
                        CONF_WEIGHT: float(user_input[CONF_WEIGHT]),
                    }
                )
                if add_another and len(self._persons) < MAX_PERSONS:
                    return await self.async_step_person()

                self._data[CONF_PERSONS] = self._persons
                self.hass.config_entries.async_update_entry(
                    self._entry, data=self._data
                )
                return self.async_create_entry(title="", data=self._data)

        return self.async_show_form(
            step_id="person",
            data_schema=_person_schema(),
            errors=errors,
            description_placeholders={"count": str(len(self._persons) + 1)},
        )
