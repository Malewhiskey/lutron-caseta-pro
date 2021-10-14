"""
Platform for sensor for button press from a Pico wireless remote.

Provides a sensor for each Pico remote with a value that changes
depending on the button press.
"""
import logging

from homeassistant.components.sensor import DOMAIN
from homeassistant.const import CONF_DEVICES, CONF_HOST, CONF_ID, CONF_MAC, CONF_NAME
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_call_later
import enum
import time

from . import (
    ATTR_AREA_NAME,
    ATTR_INTEGRATION_ID,
    CONF_AREA_NAME,
    CONF_BUTTONS,
    CONF_LONG_AND_DBL,
    CONF_LONG_TIME,
    CONF_DBL_TIME,
    Caseta,
    CasetaData,
    CasetaEntity,
)

_LOGGER = logging.getLogger(__name__)


class CasetaSensorData(CasetaData):
    """Caseta Data holder for sensor devices."""

    async def read_output(self, mode, integration, action, value):
        """Receive output value from the bridge."""
        if mode != Caseta.DEVICE:
            return

        device = self._devices.get(integration)
        if device is None:
            _LOGGER.debug(
                "No DEVICE found for value: %s %d %d %d",
                mode,
                integration,
                action,
                value,
            )
            return

        _LOGGER.debug(
            "Got DEVICE value: %s %d %d %d",
            mode,
            integration,
            action,
            value,
        )

        state = 0
        if value == Caseta.Button.PRESS:
            state = 1 << action - device.minbutton
            _LOGGER.debug("Got Button Press, updating value to: %s", state)
        elif value == Caseta.Button.RELEASE:
            _LOGGER.debug("Got Button Release, updating value to: %s", state)
        else:
            return

        device.process(state)
        #device.async_write_ha_state()


# pylint: disable=unused-argument
async def async_setup_platform(hass, config, async_add_devices, discovery_info=None):
    """Configure the platform."""
    if discovery_info is None:
        return
    bridge = Caseta(discovery_info[CONF_HOST])
    await bridge.open()

    data = CasetaSensorData(bridge)
    devices = [
        CasetaPicoRemote(pico, data, discovery_info[CONF_MAC], 
            discovery_info[CONF_LONG_AND_DBL], discovery_info[CONF_LONG_TIME], discovery_info[CONF_DBL_TIME])
        for pico in discovery_info[CONF_DEVICES]
    ]
    data.set_devices(devices)

    async_add_devices(devices)

    # register callbacks
    bridge.register(data.read_output)

    # start bridge main loop
    bridge.start(hass)

class CasetaPicoState(enum.Enum):
    idle = 0
    first_press = 1
    wait_for_second_press = 2

# Button Processor
class PicoRemoteButtonProcessor():
    """ Button Processor - Generate short/long/double press based on state transition """

    class State(enum.Enum):
        """ Button Processor state """
        idle = 0
        first_press = 1
        wait_for_second_press = 2

    class Modifier(enum.IntFlag):
        """ Button modifier """
        short = 0x00
        double = 0x40
        long = 0x80

    def __init__(self, dev, long_press_time, double_press_time):
        """Initialize button processor"""
        self.device = dev
        self.state = self.State.idle
        self.button = 0
        self.long_duration = long_press_time
        self.double_duration = double_press_time
        self.is_timeout = False
        self.cancel_long_press_timeout = None

    def reset(self):
        """Reset button processor state, cancel long press timeout"""
        self.is_timeout = False
        self.state = self.State.idle
        if self.cancel_long_press_timeout:
            self.cancel_long_press_timeout()
            self.cancel_long_press_timeout = None

    async def double_press_timeout(self, *_):
        """async function: handle double press timeout"""
        if self.state == self.State.wait_for_second_press:
            self.do_press(self.Modifier.short)
        self.is_timeout = True

    async def long_press_timeout(self, *_):
        """async function: handle long press timeout"""
        self.do_press(self.Modifier.long)

    def handle_idle_state(self, button):
        """state handler: idle state"""
        if button != 0: # 1st press
            self.is_timeout = False
            self.state = self.State.first_press
            async_call_later(self.device.hass, self.double_duration, self.double_press_timeout)
            self.cancel_long_press_timeout = \
                async_call_later(self.device.hass, self.long_duration, self.long_press_timeout)

    def handle_first_press_state(self, button):
        """state handler: first_press state"""
        if button == 0: # 1st release
            if self.is_timeout == False:
                self.state = self.State.wait_for_second_press
            else:
                self.do_press(self.Modifier.short)

    def handle_wait_for_second_press_state(self, button):
        """state handler: wait_for_second_press state"""
        if button != 0: # 2nd press
            self.do_press(self.Modifier.double)

    """ state handler map """
    state_handlers = {
        State.idle: handle_idle_state,
        State.first_press: handle_first_press_state,
        State.wait_for_second_press: handle_wait_for_second_press_state
    }
    def process(self, button):
        """process button based on current state"""
        if button != 0:
            self.button = button
        if self.state in self.state_handlers:
            self.state_handlers[self.state](self, button)

    def do_press(self, modifier):
        """generate press event"""
        self.update(self.button | int(modifier))
        self.reset()

    def update(self, state):
        """update and reset ha state"""
        _LOGGER.debug("pico button state update: button: %d, code: %d", self.button, state)
        self.device.update_state(state)
        self.device.async_write_ha_state()
        self.device.update_state(0)
        self.device.async_write_ha_state()

# pylint: disable=too-many-instance-attributes
class CasetaPicoRemote(CasetaEntity, Entity):
    """Representation of a Lutron Pico remote."""

    def __init__(self, pico, data, mac, enable_long_and_double, long_press_time, double_press_time):
        """Initialize a Lutron Pico."""
        self._data = data
        self._name = pico[CONF_NAME]
        self._area_name = None
        if CONF_AREA_NAME in pico:
            self._area_name = pico[CONF_AREA_NAME]
            # if available, prepend area name to sensor
            self._name = pico[CONF_AREA_NAME] + " " + pico[CONF_NAME]
        self._integration = int(pico[CONF_ID])
        self._buttons = pico[CONF_BUTTONS]
        self._minbutton = 100
        for button_num in self._buttons:
            if button_num < self._minbutton:
                self._minbutton = button_num
        self._state = 0
        self._mac = mac
        self._platform_domain = DOMAIN
        self.processor = PicoRemoteButtonProcessor(self, long_press_time, double_press_time)
        self.enable_long_and_double = enable_long_and_double

    @property
    def device_state_attributes(self):
        """Return device specific state attributes."""
        attr = {ATTR_INTEGRATION_ID: self._integration}
        if self._area_name:
            attr[ATTR_AREA_NAME] = self._area_name
        return attr

    @property
    def minbutton(self):
        """Return the lowest number button for this keypad."""
        return self._minbutton

    @property
    def state(self):
        """State of the Pico device."""
        return self._state

    def update_state(self, state):
        """Update state."""
        self._state = state

    def process(self, button):
        """process button state"""
        if self.enable_long_and_double:
            self.processor.process(button)
        else:
            self.update_state(button)
            self.async_write_ha_state()
