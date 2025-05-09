"""
Lutron Caseta Smart Bridge PRO and Ra2 Select Home Assistant Component.

Based on original code from jhanssen
https://github.com/jhanssen/home-assistant/tree/caseta-0.40

Additional Authors:
upsert (https://github.com/upsert)
"""

import asyncio
import json
import logging
import os.path
import weakref

import voluptuous as vol
from homeassistant.components.light import VALID_TRANSITION
from homeassistant.const import CONF_DEVICES, CONF_HOST, CONF_ID, CONF_MAC, CONF_TYPE
from homeassistant.helpers import discovery
from homeassistant.helpers.config_validation import ensure_list, positive_int, string, positive_float, boolean
from homeassistant.helpers.entity import Entity

# pylint: disable=relative-beyond-top-level
from . import casetify

_LOGGER = logging.getLogger(__name__)
_CONFIGURING = {}

DOMAIN = "lutron_caseta_pro"

ATTR_AREA_NAME = "area_name"
ATTR_INTEGRATION_ID = "integration_id"
ATTR_SCENE_ID = "scene_id"
CONF_AREA_NAME = casetify.CONF_AREA_NAME
CONF_SCENE_ID = casetify.CONF_SCENE_ID
CONF_BUTTONS = casetify.CONF_BUTTONS
CONF_BRIDGES = "bridges"
CONF_SWITCH = "switch"
CONF_COVER = "cover"
CONF_TRANSITION_TIME = "default_transition_seconds"
CONF_FAN = "fan"
DEFAULT_TYPE = "light"
CONF_LONG_AND_DBL = "enable_long_and_double"
CONF_LONG_TIME = "long_press_time"
CONF_DBL_TIME = "double_press_time"
CONF_BUTTON_COMBINATION = 'button_combination'
CONF_BTNCOMB_PICO_NAME = 'pico_name'
CONF_BTNCOMB_COMBINATIONS = 'combinations'
CONF_BTNCOMB_CODE = 'code'
CONF_BTNCOMB_COMB = 'combination'
CONF_BTNCOMB_SILENT = 'silent_press'
CONF_PRESS_TIMEOUT = 'timeout_between_press'

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_BRIDGES): vol.All(
                    ensure_list,
                    [
                        {
                            vol.Required(CONF_HOST): string,
                            vol.Optional(CONF_MAC): string,
                            vol.Optional(CONF_TRANSITION_TIME): VALID_TRANSITION,
                            vol.Optional(CONF_SWITCH): vol.All(
                                ensure_list, [positive_int]
                            ),
                            vol.Optional(CONF_COVER): vol.All(
                                ensure_list, [positive_int]
                            ),
                            vol.Optional(CONF_FAN): vol.All(
                                ensure_list, [positive_int]
                            ),
                            vol.Optional(CONF_LONG_AND_DBL): boolean,
                            vol.Optional(CONF_LONG_TIME): positive_float,
                            vol.Optional(CONF_DBL_TIME): positive_float,
                            vol.Optional(CONF_PRESS_TIMEOUT): positive_float,
                            vol.Optional(CONF_BUTTON_COMBINATION): vol.All(
                                ensure_list,
                                [
                                    {
                                        vol.Required(CONF_BTNCOMB_PICO_NAME): string,
                                        vol.Optional(CONF_BTNCOMB_SILENT): boolean,
                                        vol.Optional(CONF_BTNCOMB_COMBINATIONS): vol.All(
                                            ensure_list,
                                            [
                                                {
                                                    vol.Required(CONF_BTNCOMB_CODE): positive_int,
                                                    vol.Required(CONF_BTNCOMB_COMB): vol.All(
                                                        ensure_list,
                                                        [positive_int]
                                                    )
                                                }
                                            ]
                                        )
                                    }
                                ]
                            )
                        }
                    ],
                ),
            }
        ),
    },
    extra=vol.ALLOW_EXTRA,
)


async def request_configuration(hass, config, host, bridge):
    """Request configuration from the user to configure a host."""
    configurator = hass.components.configurator

    if host in _CONFIGURING:
        configurator.notify_errors(
            _CONFIGURING[host],
            "Failed to process Lutron Integration Report, please try again.",
        )
        return

    def setup_callback(data):
        """Set up the callback for configuration."""
        _LOGGER.debug("Entering callback for configuring host %s", host)
        # get the integration report from callback data
        integration_report_data = data.get("integration_report")
        if not integration_report_data:
            configurator.notify_errors(
                request_id, "Error reading the Integration Report. Please try again."
            )
            return False

        # parse JSON integration report
        json_int_report = json.loads(integration_report_data)

        # check for top-level object
        if not json_int_report["LIPIdList"]:
            configurator.notify_errors(
                request_id,
                "Error parsing Integration Report. "
                "Expecting it to start "
                "with 'LIPIdList'.",
            )
            return False

        str_int_report = json.dumps(json_int_report, indent=2)
        fname = get_config_file(hass, host)
        _LOGGER.debug("Writing out JSON integration report to %s", fname)
        with open(fname, "w", encoding="utf-8") as outfile:
            outfile.write(str_int_report)

        # run setup
        _LOGGER.debug("Running setup for host %s", host)
        hass.async_add_job(async_setup_bridge, hass, config, fname, bridge)
        _LOGGER.debug("Releasing configurator.")
        configurator.request_done(request_id)

        return True

    _LOGGER.info("Requesting config from user for host %s", host)

    request_id = configurator.async_request_config(
        name="Lutron Caseta Smart Bridge PRO / Ra2 Select",
        callback=setup_callback,
        description="Enter the contents of the Integration Report:",
        fields=[
            {"id": "integration_report", "name": "Integration Report", "type": "string"}
        ],
        submit_caption="Submit",
    )
    _CONFIGURING[host] = request_id


def get_config_file(hass, host):
    """Return expected path to the integration report."""
    return hass.config.path(DOMAIN + "_" + host + ".json")


async def async_setup(hass, config):
    """Initialize the component and loads the integration report."""
    if CONF_BRIDGES in config[DOMAIN]:
        for bridge in config[DOMAIN][CONF_BRIDGES]:
            host = bridge[CONF_HOST]
            # get the file name for the JSON integration report
            fname = get_config_file(hass, host)

            # check if the file exists
            if not os.path.exists(fname) or not os.path.isfile(fname):
                _LOGGER.info(
                    "Integration Report for host %s not found at location %s",
                    host,
                    fname,
                )
                hass.async_add_job(request_configuration, hass, config, host, bridge)
            else:
                _LOGGER.debug("Loading Integration Report %s", fname)
                await async_setup_bridge(hass, config, fname, bridge)

    return True


async def async_setup_bridge(hass, config, fname, bridge):
    """Initialize a bridge by loading its integration report."""
    _LOGGER.debug("Setting up bridge using Integration Report %s", fname)

    devices = await hass.async_add_executor_job(casetify.load_integration_report, fname)

    # Patch up device types from configuration.
    # All other devices will be treated as lights.
    await _patch_device_types(bridge, devices)
    _LOGGER.debug("Patched device list %s", devices)

    # sort devices based on device types
    types = {
        "sensor": [],
        "switch": [],
        "light": [],
        "cover": [],
        "scene": [],
        "fan": [],
    }
    for device in devices:
        types[device["type"]].append(device)

    # load MAC address used for unique IDs
    mac_address = None
    if CONF_MAC in bridge:
        mac_address = bridge[CONF_MAC]

    # Load default transition time, if present.
    transition_time = None
    if CONF_TRANSITION_TIME in bridge:
        transition_time = bridge[CONF_TRANSITION_TIME]
    
    # load default enable_double_and_long/long_press_time/double_press_time, if present
    enable_double_and_long = False
    if CONF_LONG_AND_DBL in bridge:
        enable_double_and_long = bridge[CONF_LONG_AND_DBL]
    long_press_time = 1.4
    if CONF_LONG_TIME in bridge:
        long_press_time = bridge[CONF_LONG_TIME]
    double_press_time = 0.8
    if CONF_DBL_TIME in bridge:
        double_press_time = bridge[CONF_DBL_TIME]
    long_press_time = max(double_press_time + 0.2, long_press_time)
    _LOGGER.debug("long_and_double_press: %d, long_press_time: %f, double_press_time: %f", enable_double_and_long,
        long_press_time, double_press_time)
    timeout_between_press = 3.0
    if CONF_PRESS_TIMEOUT in bridge:
        timeout_between_press = bridge[CONF_PRESS_TIMEOUT]

    button_combination_config = {}
    if CONF_BUTTON_COMBINATION in bridge:
        button_combination = bridge[CONF_BUTTON_COMBINATION]
        for comb_config in button_combination:
            pico_name = comb_config[CONF_BTNCOMB_PICO_NAME]
            button_combination_config[pico_name.upper()] = comb_config
    # load platform by type
    for device_type in types:
        component = device_type
        _LOGGER.debug("Loading platform %s", component)

        hass.async_create_task(
            discovery.async_load_platform(
                hass,
                component,
                DOMAIN,
                {
                    CONF_HOST: bridge[CONF_HOST],
                    CONF_MAC: mac_address,
                    CONF_DEVICES: types[device_type],
                    CONF_TRANSITION_TIME: transition_time,
                    CONF_LONG_AND_DBL: enable_double_and_long,
                    CONF_LONG_TIME: long_press_time,
                    CONF_DBL_TIME: double_press_time,
                    CONF_BUTTON_COMBINATION: button_combination_config,
                    CONF_PRESS_TIMEOUT: timeout_between_press
                },
                config,
            )
        )


async def _patch_device_types(bridge, devices):
    """Patch up the device listed based on user-provided config."""
    for device_type in [CONF_SWITCH, CONF_COVER, CONF_FAN]:
        # if type was in the configuration yaml
        if device_type in bridge:
            # for each integration ID in the configuration
            for integration_id in bridge[device_type]:
                found = False
                # Look for the integration ID in the list created from the
                # integration report.
                for existing in devices:
                    # if device ID in config matches existing device ID
                    if integration_id == existing[CONF_ID]:
                        existing[CONF_TYPE] = device_type
                        found = True
                        break
                if not found:
                    _LOGGER.warning(
                        "Integration ID %d for type %s not found in the Integration Report.",
                        integration_id,
                        device_type,
                    )


# pylint: disable=too-few-public-methods
class Caseta:
    """Caseta component class."""

    class CallbackHolder:
        """Callback holder."""

        def __init__(self, callback):
            """Create a new callback calling the method @callback."""
            obj = callback.__self__
            attr = callback.__func__.__name__
            self.wref = weakref.ref(obj, self.object_deleted)
            self.callback_attr = attr
            self.token = None

        async def call(self, *args, **kwargs):
            """Call the callback referenced by this object."""
            obj = self.wref()
            if obj:
                attr = getattr(obj, self.callback_attr)
                await attr(*args, **kwargs)

        def object_deleted(self, wref):
            """Delete the callback when it expires."""
            pass

    class CasetaBridge:
        """Inner class for handling Lutron bridge communication."""

        host_list = {}

        def __init__(self, host):
            """Initialize bridge."""
            self._host = host
            self._casetify = None
            self._hass = None
            self._callbacks = []

        def __str__(self):
            """Return self plus host name."""
            return repr(self) + self._host

        async def _read_next(self):
            """Read and process a value from the Lutron interface."""
            read_response = await self._casetify.read()
            mode = read_response[0]
            integration = read_response[1]
            action = read_response[2]
            value = read_response[3]
            if mode is None:
                self._hass.loop.create_task(self._read_next())
                return
            _LOGGER.debug(
                "Read value for host %s: %s %d %d %f",
                self._host,
                mode,
                integration,
                action,
                value,
            )
            # walk callbacks
            for callback in self._callbacks:
                await callback.call(mode, integration, action, value)
            self._hass.loop.create_task(self._read_next())

        async def _reconnect(self):
            """Attempt to re-connect to the Lutron bridge."""
            if not self._casetify.is_connected():
                await self._casetify.open(self._host)
                if not self._casetify.is_connected():
                    _LOGGER.debug("Waiting to reconnect.")
                else:
                    _LOGGER.debug("Re-connected to the Lutron bridge.")

        async def _ping(self):
            """Send a ping to the Caseta interface."""
            await asyncio.sleep(60)
            await self._casetify.ping()

            # check the connection, reconnect if needed
            if not self._casetify.is_connected():
                _LOGGER.debug(
                    "Lutron bridge not connected. Scheduling a reconnect attempt."
                )
                self._hass.loop.create_task(self._reconnect())

            self._hass.loop.create_task(self._ping())

        async def open(self):
            """Open a connection to the Lutron bridge."""
            if self._casetify is not None:
                # connection already open
                return True
            _LOGGER.info("Opening connection to host %s", self._host)
            self._casetify = casetify.Casetify()
            await self._casetify.open(self._host)
            return True

        async def write(self, mode, integration, action, value, *args):
            """Write a value to the Lutron bridge."""
            if self._casetify is None:
                return False
            await self._casetify.write(mode, integration, action, value, *args)
            return True

        async def query(self, mode, integration, action):
            """Query a device value from the Lutron bridge."""
            if self._casetify is None:
                return False
            await self._casetify.query(mode, integration, action)
            return True

        def register(self, callback):
            """Register a callback."""
            self._callbacks.append(Caseta.CallbackHolder(callback))

        def start(self, hass):
            """Start the bridge running loop."""
            if self._hass is None:
                _LOGGER.debug("Starting Lutron component for host %s", self._host)
                self._hass = hass
                hass.loop.create_task(self._read_next())
                hass.loop.create_task(self._ping())

        @property
        def host(self):
            """Return the host name."""
            return self._host

    OUTPUT = casetify.Casetify.OUTPUT
    DEVICE = casetify.Casetify.DEVICE

    Action = casetify.Casetify.Action
    Button = casetify.Casetify.Button

    def __init__(self, host):
        """Initialize Caseta instance."""
        instance = None
        if host in Caseta.CasetaBridge.host_list:
            instance = Caseta.CasetaBridge.host_list[host]
        else:
            instance = Caseta.CasetaBridge(host)
            Caseta.CasetaBridge.host_list[host] = instance
        super(Caseta, self).__setattr__("instance", instance)

    def __getattr__(self, name):
        """Return getter on the instance."""
        return getattr(self.instance, name)

    def __setattr__(self, name, value):
        """Return setter on the instance."""
        setattr(self.instance, name, value)


class CasetaEntity(Entity):
    """Base entity."""

    @property
    def should_poll(self):
        """No need to poll for updates."""
        return False

    @property
    def integration(self):
        """Return the integration ID."""
        return self._integration

    @property
    def name(self):
        """Return the display name of this device."""
        return self._name

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        if self._mac is not None:
            # return "{}_{}_{}_{}".format(
                # DOMAIN, self._platform_domain, self._mac, self._integration
            # )
            return f"{DOMAIN}_{self._platform_domain}_{self._mac}_{self._integration}"
        return None


class CasetaData:
    """Caseta Data holder."""

    def __init__(self, caseta):
        """Initialize the data holder."""
        self.caseta = caseta
        self._devices = []

    @property
    def devices(self):
        """Return the device list."""
        return self._devices

    def set_devices(self, devices):
        """Set the device list."""
        self._devices = {device.integration: device for device in devices}

    async def read_output(self, mode, integration, action, value):
        """Receive output value from the bridge."""
        # Expect: ~OUTPUT,Integration ID,Action Number,Parameters
        if mode != Caseta.OUTPUT:
            return

        device = self._devices.get(integration)
        if device is None:
            return

        _LOGGER.debug(
            "Got OUTPUT value: %s %d %d %f",
            mode,
            integration,
            action,
            value,
        )
        if action != Caseta.Action.SET:
            return

        # update zone level, e.g. 90.00
        device.update_state(value)

        if device.hass is not None:
            device.async_write_ha_state()
