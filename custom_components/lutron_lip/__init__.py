"""Component for interacting with a Lutron RadioRA 2 system."""

from dataclasses import dataclass
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import homeassistant.helpers.config_validation as cv
from homeassistant.util import slugify

from .aiolip import Button, Led, LutronController, OccupancyGroup, Output, Sysvar
from .const import (
    CONF_REFRESH_DATA,
    CONF_SUGGEST_AREAS,
    CONF_USE_AREA_FOR_DEVICE_NAME,
    CONF_USE_FULL_PATH,
    CONF_USE_RADIORA_MODE,
    CONF_VARIABLE_IDS,
    DOMAIN,
    LUTRON_DATA_FILE,
    first_device_by_identifier,
)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.COVER,
    Platform.EVENT,
    Platform.FAN,
    Platform.LIGHT,
    Platform.SCENE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

_LOGGER = logging.getLogger(__name__)


class LutronException(Exception):
    """Top level module exception."""


class IntegrationIdExistsError(LutronException):
    """Asserted when there's an attempt to register a duplicate integration id."""


# Attribute on events that indicates what action was taken with the button.
ATTR_ACTION = "action"
ATTR_FULL_ID = "full_id"
ATTR_UUID = "uuid"

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_HOST): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
                vol.Required(CONF_USERNAME): cv.string,
                vol.Required(CONF_REFRESH_DATA, default=True): cv.boolean,
                vol.Required(CONF_USE_FULL_PATH, default=False): cv.boolean,
                vol.Required(CONF_USE_AREA_FOR_DEVICE_NAME, default=False): cv.boolean,
                vol.Required(CONF_USE_RADIORA_MODE, default=False): cv.boolean,
                vol.Optional(CONF_VARIABLE_IDS, default=""): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


@dataclass(slots=True, kw_only=True)
class LutronData:
    """Storage class for platform global data."""

    controller: LutronController
    binary_sensors: list[OccupancyGroup]
    covers: list[Output]
    fans: list[Output]
    lights: list[Output]
    buttons: list[Button]
    scenes: list[Button]
    leds: list[Led]
    switches: list[Output]
    variables: list[Sysvar]


def get_entry_value(entry: ConfigEntry, key: str, default=None):
    """Get the entry from options if available, else return it from the original data."""
    return entry.options.get(key, entry.data.get(key, default))


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up the Lutron integration."""

    host = config_entry.data[CONF_HOST]
    uid = config_entry.data[CONF_USERNAME]
    pwd = config_entry.data[CONF_PASSWORD]
    refresh_data = get_entry_value(config_entry, CONF_REFRESH_DATA, True)
    use_full_path = get_entry_value(
        config_entry, CONF_USE_FULL_PATH, False
    )  # use also the location (i.e., the parent area) for the area name
    use_area_for_device_name = get_entry_value(
        config_entry, CONF_USE_AREA_FOR_DEVICE_NAME, False
    )  # use area name in the device name
    use_radiora_mode = get_entry_value(
        config_entry, CONF_USE_RADIORA_MODE, False
    )  # use compatibility mode for old integration
    suggest_areas = get_entry_value(config_entry, CONF_SUGGEST_AREAS, True)
    variable_ids_str = get_entry_value(config_entry, CONF_VARIABLE_IDS, "")
    variable_ids = [
        int(v.strip()) for v in variable_ids_str.split(",") if v.strip().isdigit()
    ]

    lutron_data_file = hass.config.path(LUTRON_DATA_FILE)

    lutron_controller = LutronController(
        hass, host, uid, pwd, use_full_path, use_area_for_device_name, use_radiora_mode,
        suggest_areas=suggest_areas,
    )
    await hass.async_add_executor_job(
        lambda: lutron_controller.load_xml_db(
            lutron_data_file, refresh_data, variable_ids=variable_ids
        )
    )
    await lutron_controller.connect()
    _LOGGER.info("Connected to main repeater at %s", host)

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    # select buttons that should be linked to scenes
    # this logic is from radiora integration.
    # "DualAction","AdvancedConditional","SimpleConditional", are excluded
    RADIORA_VALID_BUTTON_TYPES = [
        "SingleAction",
        "Toggle",
        "SingleSceneRaiseLower",
        "MasterRaiseLower",
        "AdvancedToggle",
    ]

    entry_data = LutronData(
        controller=lutron_controller,
        binary_sensors=[],
        buttons=[],
        covers=[],
        fans=[],
        lights=[],
        leds=[],
        scenes=[],
        switches=[],
        variables=[],
    )
    # Sort our devices into types
    _LOGGER.debug("Start adding devices")
    for area in lutron_controller.areas:
        _LOGGER.debug("Working on area %s", area.name)
        for output in area.outputs:
            _LOGGER.debug("Working on output %s", output.output_type)
            if output.is_motor or output.is_shade:
                entry_data.covers.append(output)
                platform = Platform.COVER
            elif output.is_fan:
                entry_data.fans.append(output)
                platform = Platform.FAN
            elif output.is_light:
                entry_data.lights.append(output)
                platform = Platform.LIGHT
            else:
                entry_data.switches.append(output)
                platform = Platform.SWITCH

            _async_check_entity_unique_id(
                hass,
                entity_registry,
                platform,
                output.uuid,
                output.legacy_uuid,
                entry_data.controller.guid,
            )
            _async_check_device_identifiers(
                hass,
                device_registry,
                output.uuid,
                output.legacy_uuid,
                entry_data.controller.guid,
            )

        for keypad in area.keypads:
            _async_check_device_identifiers(
                hass,
                device_registry,
                keypad.uuid,
                keypad.legacy_uuid,
                entry_data.controller.guid,
            )
            for button in keypad.buttons:
                # If the Lutron button has actions assigned to it,
                # add to buttons for the corresponding events and
                # add it as a scene, but in compatibility mode
                # exclude Unknown buttons
                #
                # Add leds controlled by integration as a light device
                # RadioRa mode adds valid leds as switches

                if button.button_type:
                    entry_data.buttons.append(button)

                    _async_check_entity_unique_id(
                        hass,
                        entity_registry,
                        Platform.EVENT,
                        button.uuid,
                        button.legacy_uuid,
                        entry_data.controller.guid,
                    )

                if (
                    not button.name.startswith("Unknown")
                    and button.button_type in RADIORA_VALID_BUTTON_TYPES
                ) or not use_radiora_mode:
                    entry_data.scenes.append(button)

                    _async_check_entity_unique_id(
                        hass,
                        entity_registry,
                        Platform.SCENE,
                        button.uuid,
                        button.legacy_uuid,
                        entry_data.controller.guid,
                    )

            for led in keypad.leds:
                if led.button is not None:
                    entry_data.leds.append(led)
                    _async_check_entity_unique_id(
                        hass,
                        entity_registry,
                        Platform.SWITCH,
                        led.uuid,
                        led.legacy_uuid,
                        entry_data.controller.guid,
                    )

        # exclude occupancy_group not linked to an area
        if (
            area.occupancy_group is not None
            and area.occupancy_group.integration_id != 0
        ):
            entry_data.binary_sensors.append(area.occupancy_group)
            platform = Platform.BINARY_SENSOR
            _async_check_entity_unique_id(
                hass,
                entity_registry,
                platform,
                area.occupancy_group.uuid,
                area.occupancy_group.legacy_uuid,
                entry_data.controller.guid,
            )
            _async_check_device_identifiers(
                hass,
                device_registry,
                area.occupancy_group.uuid,
                area.occupancy_group.legacy_uuid,
                entry_data.controller.guid,
            )
    # check variables
    for variable in lutron_controller.variables:
        _LOGGER.debug("Working on variable %s", variable.name)
        platform = Platform.SELECT
        entry_data.variables.append(variable)

        _async_check_entity_unique_id(
            hass,
            entity_registry,
            platform,
            "",
            variable.legacy_uuid,
            entry_data.controller.guid,
        )

    # create the device for the Controller
    controller_device = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, lutron_controller.guid)},
        manufacturer="Lutron",
        name="Lutron Controller",
    )
    # Entities parent themselves to this device by registry id (2026.9+).
    lutron_controller.ha_device_id = controller_device.id

    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = entry_data
    # Options (Suggest areas, naming, variables) are read only here, at setup:
    # without this a changed option sat unused until the next restart.
    config_entry.async_on_unload(config_entry.add_update_listener(_async_options_updated))

    _setup_button_events(hass, entry_data)

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    return True


def _async_check_entity_unique_id(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    platform: str,
    uuid: str,
    legacy_uuid: str,
    controller_guid: str,
) -> None:
    """If uuid becomes available update to use it."""
    if not uuid:
        return

    unique_id = f"{controller_guid}_{legacy_uuid}"
    entity_id = entity_registry.async_get_entity_id(
        domain=platform, platform=DOMAIN, unique_id=unique_id
    )

    if entity_id:
        new_unique_id = f"{controller_guid}_{uuid}"
        _LOGGER.debug("Updating entity id from %s to %s", unique_id, new_unique_id)
        entity_registry.async_update_entity(entity_id, new_unique_id=new_unique_id)


def _async_check_device_identifiers(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    uuid: str,
    legacy_uuid: str,
    controller_guid: str,
) -> None:
    """If uuid becomes available, update to use it."""
    if not uuid:
        return

    unique_id = f"{controller_guid}_{legacy_uuid}"
    device = first_device_by_identifier(device_registry, {(DOMAIN, unique_id)})
    if device:
        new_unique_id = f"{controller_guid}_{uuid}"
        _LOGGER.debug("Updating device id from %s to %s", unique_id, new_unique_id)
        device_registry.async_update_device(
            device.id, new_identifiers={(DOMAIN, new_unique_id)}
        )


def _setup_button_events(hass: HomeAssistant, entry_data: LutronData) -> None:
    """Register lutron_event bus callbacks for all buttons (no HA entities created)."""
    _action_to_event = {
        3: "press",
        4: "release",
        5: "hold",
        6: "double_tap",
        32: "hold_release",
    }
    controller = entry_data.controller

    for button in entry_data.buttons:
        if controller.use_radiora_mode:
            keypad_name = button.keypad.name
        else:
            keypad_name = f"keypad {button.keypad.integration_id}"

        component_name = button.component_name

        area = button.area
        if controller.use_area_for_device_name and area:
            area_prefix = (
                f"{area.location} {area.name}"
                if controller.use_full_path
                else area.name
            )
            device_name = f"{area_prefix} {keypad_name}"
        else:
            device_name = keypad_name

        full_id = slugify(f"{device_name}: {component_name}")
        btn_id = slugify(f"{keypad_name}: {component_name}")
        btn_uuid = button.uuid

        def _make_cb(fid, bid, uuid):
            def _cb(action_number):
                event_type = _action_to_event.get(action_number)
                if event_type:
                    hass.bus.fire(
                        "lutron_event",
                        {
                            "id": bid,
                            ATTR_ACTION: event_type,
                            ATTR_FULL_ID: fid,
                            ATTR_UUID: uuid,
                        },
                    )
            return _cb

        controller.subscribe(button.integration_id, button.component_number, _make_cb(full_id, btn_id, btn_uuid))


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply changed options by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Clean up resources and entities associated with the integration."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    entry_data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    # Close the LIP session too, or a reload leaves the old one running beside
    # the new one: two logins on the processor and every event handled twice.
    lip = getattr(getattr(entry_data, "controller", None), "lip", None)
    if lip is not None:
        await lip.async_stop()
    return True
