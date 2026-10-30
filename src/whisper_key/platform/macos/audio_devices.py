# audio_devices.py (macOS)
# Notifies the app when CoreAudio's device list changes (USB interface plugged
# in or pulled out). PortAudio enumerates devices once at startup and never
# notices hot-plugs on its own, so the audio recorder relies on this signal to
# rebuild its stream on the right device.

import ctypes
import logging
from typing import Callable

logger = logging.getLogger(__name__)

# --- CoreAudio constants and types ------------------------------------------

def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode('ascii'), 'big')

K_AUDIO_OBJECT_SYSTEM_OBJECT = 1
K_AUDIO_HARDWARE_PROPERTY_DEVICES = _fourcc('dev#')
K_AUDIO_HARDWARE_PROPERTY_DEFAULT_INPUT_DEVICE = _fourcc('dIn ')
K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL = _fourcc('glob')
K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN = 0


class _AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ('mSelector', ctypes.c_uint32),
        ('mScope', ctypes.c_uint32),
        ('mElement', ctypes.c_uint32),
    ]


_PropertyListenerProc = ctypes.CFUNCTYPE(
    ctypes.c_int32,                                     # OSStatus
    ctypes.c_uint32,                                    # AudioObjectID
    ctypes.c_uint32,                                    # number of addresses
    ctypes.POINTER(_AudioObjectPropertyAddress),        # changed addresses
    ctypes.c_void_p,                                    # client data
)

_core_audio = ctypes.CDLL('/System/Library/Frameworks/CoreAudio.framework/CoreAudio')
for _fn in (_core_audio.AudioObjectAddPropertyListener, _core_audio.AudioObjectRemovePropertyListener):
    _fn.argtypes = [ctypes.c_uint32, ctypes.POINTER(_AudioObjectPropertyAddress),
                    _PropertyListenerProc, ctypes.c_void_p]
    _fn.restype = ctypes.c_int32

# The device list covers plug/unplug; the default input covers the user picking
# a different input in System Settings while we follow the system default.
_WATCHED_PROPERTIES = (
    K_AUDIO_HARDWARE_PROPERTY_DEVICES,
    K_AUDIO_HARDWARE_PROPERTY_DEFAULT_INPUT_DEVICE,
)


# --- Public API --------------------------------------------------------------

# Calls on_change (on a CoreAudio thread) whenever an audio device appears or
# disappears, or the default input changes. Returns a function that unsubscribes.
def watch_device_changes(on_change: Callable[[], None]) -> Callable[[], None]:
    def _listener(_object_id, _count, _addresses, _client_data):
        try:
            on_change()
        except Exception as e:
            logger.error(f"Audio device change handler failed: {e}")
        return 0

    # Keep a reference to the ctypes callback: if it is garbage collected,
    # CoreAudio calls into freed memory.
    listener_proc = _PropertyListenerProc(_listener)
    addresses = [
        _AudioObjectPropertyAddress(selector, K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL,
                                    K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN)
        for selector in _WATCHED_PROPERTIES
    ]

    registered = []
    for address in addresses:
        status = _core_audio.AudioObjectAddPropertyListener(
            K_AUDIO_OBJECT_SYSTEM_OBJECT, ctypes.byref(address), listener_proc, None)
        if status == 0:
            registered.append(address)
        else:
            logger.warning(f"Could not watch CoreAudio property {address.mSelector:#x} (status {status})")

    def unsubscribe():
        for address in registered:
            _core_audio.AudioObjectRemovePropertyListener(
                K_AUDIO_OBJECT_SYSTEM_OBJECT, ctypes.byref(address), listener_proc, None)
        registered.clear()

    return unsubscribe
