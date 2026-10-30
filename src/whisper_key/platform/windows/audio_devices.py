# audio_devices.py (Windows)
# Device-change notifications are not wired up on Windows yet. The audio
# recorder still recovers from a lost device via its stall watchdog and stream
# errors; it just won't switch back to a re-plugged device on its own.

from typing import Callable


def watch_device_changes(on_change: Callable[[], None]) -> Callable[[], None]:
    return lambda: None
