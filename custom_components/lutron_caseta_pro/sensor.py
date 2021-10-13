"""
Platform for sensor for button press from a Pico wireless remote.

Provides a sensor for each Pico remote with a value that changes
depending on the button press.
"""
import logging

from homeassistant.components.sensor import DOMAIN
from homeassistant.const import CONF_DEVICES, CONF_HOST, CONF_ID, CONF_MAC, CONF_NAME
from homeassistant.helpers.entity import Entity
import enum
import time
from threading import Timer, Lock

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
    wait_for_second = 2
    single_click = 3
    double_click = 4
    long_click = 5

# Button Processor
class PicoRemoteButtonProcessor():
    class State(enum.Enum):
        idle = 0
        first_press = 1
        wait_for_second = 2
        single_click = 3
        double_click = 4
        long_click = 5

    def __init__(self, dev, long_press_time, double_press_time):
        self.device = dev
        self.state = self.State.idle
        self.button_state = 0
        self.timer = None
        self.lock = Lock()
        self.first_press_time = 0
        self.long_duration = long_press_time
        self.double_duration = double_press_time

    def timeout(self):
        self.lock.acquire()
        if self.state == self.State.wait_for_second:
            self.update(self.button_state)
            self.state = self.State.idle
        self.timer = None
        self.lock.release()

    def process(self, button_state):
        self.lock.acquire()
        if button_state != 0:
            self.button_state = button_state
            
        if self.state == self.State.idle:
            if button_state != 0: # 1st press
                self.first_press_time = time.time()
                self.state = self.State.first_press
                self.timer = Timer(self.double_duration, self.timeout)
                self.timer.start()
        elif self.state == self.State.first_press:
            if button_state == 0: # 1st release
                if self.timer != None:
                    self.state = self.State.wait_for_second
                else:
                    self.single_click()
        elif self.state == self.State.wait_for_second:
            if button_state != 0: # 2nd press
                self.double_click()

        self.lock.release()

    def single_click(self):
        if (time.time() - self.first_press_time) > self.long_duration:
            self.update(self.button_state | 0x80)
        else:
            self.update(self.button_state)
        self.state = self.State.idle

    def double_click(self):
        self.update(self.button_state | 0x40)
        self.state = self.State.idle

    def update(self, state):
        _LOGGER.debug("pico button state update: %d", state)
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

    def process(self, button_state):
        if self.enable_long_and_double:
            _LOGGER.debug("long_and_double processing enabled")
            self.processor.process(button_state)
        else:
            _LOGGER.debug("long_and_double processing disabled")
            self.update_state(button_state)
            self.async_write_ha_state()
