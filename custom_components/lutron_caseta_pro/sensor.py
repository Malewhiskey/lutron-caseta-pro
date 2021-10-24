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
    CONF_PRESS_TIMEOUT,
    CONF_BUTTON_COMBINATION,
    CONF_BTNCOMB_PICO_NAME,
    CONF_BTNCOMB_COMBINATIONS,
    CONF_BTNCOMB_CODE,
    CONF_BTNCOMB_COMB,
    CONF_BTNCOMB_SILENT,
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
            discovery_info[CONF_LONG_AND_DBL], discovery_info[CONF_LONG_TIME], discovery_info[CONF_DBL_TIME],
            discovery_info[CONF_BUTTON_COMBINATION], discovery_info[CONF_PRESS_TIMEOUT])
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

    def __init__(self, dev, long_press_time, double_press_time, comb_config, press_timeout):
        """Initialize button processor"""
        self.device = dev
        self.state = self.State.idle
        self.button = 0
        self.timeout_callback = (self.double_press_timeout, self.long_press_timeout)
        self.timeout_time = (double_press_time, long_press_time)
        self.timeout_handle = [None, None]
        self.timeout_flags = [False, False]
        # button combination
        self.silent_press = CONF_BTNCOMB_SILENT in comb_config and comb_config[CONF_BTNCOMB_SILENT]
        self.combinations = {}
        self.max_history_length = 0
        if CONF_BTNCOMB_COMBINATIONS in comb_config:
            for combination in comb_config[CONF_BTNCOMB_COMBINATIONS]:
                comb_list = combination[CONF_BTNCOMB_COMB]
                self.max_history_length = max(len(comb_list), self.max_history_length)
                self.combinations[str(comb_list)[1:-1]] = combination[CONF_BTNCOMB_CODE]
        self.key_history = []
        self.last_press_time = time.time()
        self.press_timeout = press_timeout
        
    def reset(self):
        """Reset button processor state, cancel long press timeout"""
        self.timeout_flags = [False, False]
        self.state = self.State.idle
        self.button = 0
        for cancel_timeout in self.timeout_handle:
            cancel_timeout()

    async def double_press_timeout(self, *_):
        """async function: handle double press timeout"""
        if self.state == self.State.wait_for_second_press:
            self.do_press(self.Modifier.short)
        self.timeout_flags[0] = True

    async def long_press_timeout(self, *_):
        """async function: handle long press timeout"""
        if self.state == self.State.first_press:
            self.do_press(self.Modifier.long)
        self.timeout_flags[1] = True

    def handle_idle_state(self, button):
        """state handler: idle state"""
        if button != 0: # 1st press
            self.timeout_flags = [False, False]
            self.state = self.State.first_press
            for i in range(0, 2):
                self.timeout_handle[i] = \
                    async_call_later(self.device.hass, self.timeout_time[i], self.timeout_callback[i])

    def handle_first_press_state(self, button):
        """state handler: first_press state"""
        if button == 0: # 1st release
            if self.timeout_flags[0] == False:
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
            if self.button != 0 and self.button != button:
                # different button pressed while button processor still handle previous button
                self.do_press(self.Modifier.short)
            self.button = button
        if self.state in self.state_handlers:
            self.state_handlers[self.state](self, button)

    def do_press(self, modifier):
        """generate press event"""
        modified_button = self.button | int(modifier)        
        if not self.silent_press:
            self.update(modified_button)
        self.reset()
        # check key combinations
        if self.combinations != {}:
            self.process_combination(modified_button)

    def process_combination(self, modified_button):
        cur_time = time.time()
        # check timeout
        if (cur_time - self.last_press_time) > self.press_timeout:
            self.key_history = [modified_button]
        else:
            # append to the end of key history
            self.key_history += [modified_button]
        self.last_press_time = cur_time
        # check if match any combination
        key_history_str = str(self.key_history)[1:-1]            
        if key_history_str in self.combinations:
            code = self.combinations[key_history_str]
            self.update(code)
            self.key_history = []       
        # clear history if equal self.max_history_length (regardless matched or not)
        if len(self.key_history) >= self.max_history_length:
            self.key_history = []

    def update(self, state):
        """update and reset ha state"""
        _LOGGER.debug("[%s] pico button state update: button: %d, code: %d", self.device._name, self.button, state)
        self.device.update_state(state)
        self.device.async_write_ha_state()
        self.device.update_state(0)
        self.device.async_write_ha_state()

# pylint: disable=too-many-instance-attributes
class CasetaPicoRemote(CasetaEntity, Entity):
    """Representation of a Lutron Pico remote."""

    def __init__(self, pico, data, mac, enable_long_and_double, long_press_time, double_press_time, comb_config, press_timeout):
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
        btn_comb_config = comb_config[self._name.upper()] if self._name.upper() in comb_config else {}
        self.processor = PicoRemoteButtonProcessor(self, long_press_time, double_press_time, btn_comb_config, press_timeout)
        self.enable_long_and_double = enable_long_and_double
        _LOGGER.debug(f'CasetaPicoRemote name: {self._name}')

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
