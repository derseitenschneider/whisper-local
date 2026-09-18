# platform/macos/hotkeys.py
# Global hotkey detection. Two mechanisms, in order of preference:
#
#   1. A Quartz CGEventTap on its own run-loop thread. This is the only way
#      to CONSUME a matched keystroke — without it, a shortcut like
#      Option+Space fires dictation and still types a space into the focused
#      app (issue #4). Needs Accessibility permission; macOS may also disable
#      a tap that runs long, so the callback re-enables itself.
#   2. NSEvent.addGlobalMonitorForEventsMatchingMask_ as fallback when the tap
#      can't be created (normally missing permission). Global NSEvent monitors
#      can observe but never suppress, so shortcuts still fire and the
#      underlying key still reaches the app.
#
# Modifier (flags-changed) events are always passed through — suppressing a
# bare Ctrl or Option would break normal typing.
# Windows mirror: platform/windows/hotkeys.py.
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable

from AppKit import NSEvent
from Quartz import (
    CFMachPortCreateRunLoopSource,
    CFMachPortInvalidate,
    CFRunLoopAddSource,
    CFRunLoopGetCurrent,
    CFRunLoopRun,
    CFRunLoopStop,
    CGEventGetIntegerValueField,
    CGEventMaskBit,
    CGEventTapCreate,
    CGEventTapEnable,
    kCFRunLoopCommonModes,
    kCGEventFlagsChanged,
    kCGEventKeyDown,
    kCGEventKeyUp,
    kCGEventTapDisabledByTimeout,
    kCGEventTapDisabledByUserInput,
    kCGEventTapOptionDefault,
    kCGHeadInsertEventTap,
    kCGKeyboardEventKeycode,
    kCGSessionEventTap,
)

from .keycodes import KEY_CODES

logger = logging.getLogger(__name__)

NSKeyDownMask = 1 << 10
NSKeyUpMask = 1 << 11
NSFlagsChangedMask = 1 << 12

MODIFIER_FLAGS = {
    'ctrl': 1 << 18,
    'control': 1 << 18,
    'cmd': 1 << 20,
    'command': 1 << 20,
    'option': 1 << 19,
    'alt': 1 << 19,
    'shift': 1 << 17,
    'fn': 1 << 23,
    'function': 1 << 23,
}

MODIFIER_MASK = (1 << 18) | (1 << 20) | (1 << 19) | (1 << 17) | (1 << 23)


@dataclass
class ParsedBinding:
    original: str
    modifiers: int
    keycode: int | None
    press_callback: Callable
    release_callback: Callable | None
    is_active: bool = field(default=False)


class ModifierStateTracker:
    def __init__(self):
        self.previous_flags = 0

    def update(self, new_flags: int) -> tuple[int, int, int, int]:
        new_flags = new_flags & MODIFIER_MASK
        old_flags = self.previous_flags
        pressed = new_flags & ~old_flags
        released = old_flags & ~new_flags
        self.previous_flags = new_flags
        return old_flags, new_flags, pressed, released

    def reset(self):
        self.previous_flags = 0


_monitor = None
_event_tap = None
_tap_run_loop = None
_tap_thread = None
_tap_ready = threading.Event()
_suppressed_keycodes: set[int] = set()
_bindings: list[ParsedBinding] = []
_state = ModifierStateTracker()


def _parse_hotkey_string(hotkey_str: str) -> tuple[int, int | None]:
    parts = [p.strip().lower() for p in hotkey_str.split('+')]

    modifiers = 0
    keycode = None

    for part in parts:
        if part in ('win', 'window', 'windows', 'super'):
            continue

        if part in MODIFIER_FLAGS:
            modifiers |= MODIFIER_FLAGS[part]
        elif part in KEY_CODES:
            keycode = KEY_CODES[part]
        else:
            logger.warning(f"Unknown key in hotkey string: {part}")

    return modifiers, keycode


def _parse_binding(binding: list) -> ParsedBinding:
    hotkey_str = binding[0]
    press_cb = binding[1]
    release_cb = binding[2] if len(binding) > 2 else None

    modifiers, keycode = _parse_hotkey_string(hotkey_str)

    return ParsedBinding(
        original=hotkey_str,
        modifiers=modifiers,
        keycode=keycode,
        press_callback=press_cb,
        release_callback=release_cb,
    )


def _handle_flags_changed(event):
    old_flags, new_flags, pressed, released = _state.update(event.modifierFlags())

    for binding in _bindings:
        if binding.keycode is not None:
            continue

        if new_flags == binding.modifiers and old_flags != binding.modifiers:
            logger.debug(f"Modifier-only hotkey pressed: {binding.original}")
            binding.is_active = True
            try:
                threading.Thread(target=binding.press_callback, daemon=True).start()
            except Exception as e:
                logger.error(f"Error in press callback for {binding.original}: {e}")

        elif binding.is_active and (released & binding.modifiers):
            logger.debug(f"Modifier-only hotkey released: {binding.original}")
            binding.is_active = False
            if binding.release_callback:
                try:
                    threading.Thread(target=binding.release_callback, daemon=True).start()
                except Exception as e:
                    logger.error(f"Error in release callback for {binding.original}: {e}")


def _handle_key_down(event):
    current_flags = event.modifierFlags() & MODIFIER_MASK
    key_code = event.keyCode()

    logger.debug(f"KeyDown: keycode={key_code}, flags={current_flags:#x}")

    for binding in _bindings:
        if binding.keycode is None:
            continue

        if key_code == binding.keycode and current_flags == binding.modifiers:
            logger.debug(f"Traditional hotkey pressed: {binding.original}")
            binding.is_active = True
            try:
                threading.Thread(target=binding.press_callback, daemon=True).start()
            except Exception as e:
                logger.error(f"Error in press callback for {binding.original}: {e}")
            return True
    return False


def _handle_key_up(key_code: int):
    # Release matches on keycode alone: the modifiers may already be up.
    for binding in _bindings:
        if binding.keycode != key_code or not binding.is_active:
            continue
        binding.is_active = False
        if binding.release_callback:
            logger.debug(f"Traditional hotkey released: {binding.original}")
            try:
                threading.Thread(target=binding.release_callback, daemon=True).start()
            except Exception as e:
                logger.error(f"Error in release callback for {binding.original}: {e}")


def _handle_event(event):
    event_type = event.type()

    if event_type == 12:  # NSFlagsChanged
        _handle_flags_changed(event)
    elif event_type == 10:  # NSKeyDown
        if not event.isARepeat():
            _handle_key_down(event)
    elif event_type == 11:  # NSKeyUp
        _handle_key_up(event.keyCode())


def _event_tap_callback(proxy, event_type, event, refcon):
    """Dispatch hotkeys and consume matching regular-key events.

    NSEvent's global monitor can observe but cannot suppress keystrokes. A Quartz
    event tap can return None for a matched key, preventing e.g. Option+Space
    from inserting a space into the focused application.
    """
    if event_type in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
        if _event_tap is not None:
            CGEventTapEnable(_event_tap, True)
        return event

    try:
        ns_event = NSEvent.eventWithCGEvent_(event)
        if event_type == kCGEventFlagsChanged:
            _handle_flags_changed(ns_event)
            return event

        key_code = int(CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode))
        if event_type == kCGEventKeyDown:
            # Ignore key-repeat callbacks but continue consuming the held key.
            if key_code in _suppressed_keycodes:
                return None
            if _handle_key_down(ns_event):
                _suppressed_keycodes.add(key_code)
                return None
        elif event_type == kCGEventKeyUp and key_code in _suppressed_keycodes:
            _suppressed_keycodes.discard(key_code)
            _handle_key_up(key_code)
            return None
    except Exception as e:
        logger.error(f"Error handling Quartz hotkey event: {e}")

    return event


def _run_event_tap():
    global _event_tap, _tap_run_loop
    mask = (
        CGEventMaskBit(kCGEventKeyDown)
        | CGEventMaskBit(kCGEventKeyUp)
        | CGEventMaskBit(kCGEventFlagsChanged)
    )
    _event_tap = CGEventTapCreate(
        kCGSessionEventTap,
        kCGHeadInsertEventTap,
        kCGEventTapOptionDefault,
        mask,
        _event_tap_callback,
        None,
    )
    if _event_tap is None:
        _tap_ready.set()
        return

    source = CFMachPortCreateRunLoopSource(None, _event_tap, 0)
    _tap_run_loop = CFRunLoopGetCurrent()
    CFRunLoopAddSource(_tap_run_loop, source, kCFRunLoopCommonModes)
    CGEventTapEnable(_event_tap, True)
    _tap_ready.set()
    CFRunLoopRun()


def register(bindings: list):
    global _bindings
    _bindings = [_parse_binding(b) for b in bindings]
    logger.info(f"Registered {len(_bindings)} hotkey bindings")
    for b in _bindings:
        binding_type = "modifier-only" if b.keycode is None else "traditional"
        logger.debug(f"  {b.original} -> modifiers={b.modifiers:#x}, keycode={b.keycode} ({binding_type})")


def start():
    global _monitor, _tap_thread
    _state.reset()
    _suppressed_keycodes.clear()

    _tap_ready.clear()
    _tap_thread = threading.Thread(target=_run_event_tap, daemon=True, name='macos-hotkey-event-tap')
    _tap_thread.start()
    _tap_ready.wait(timeout=1.0)

    if _event_tap is not None:
        logger.info("Quartz hotkey event tap started (matching keys are suppressed)")
        return

    # Read-only fallback: shortcuts still fire, but their regular key is also
    # delivered to the focused app. This path is normally a permissions issue.
    mask = NSKeyDownMask | NSKeyUpMask | NSFlagsChangedMask
    _monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(mask, _handle_event)

    if _monitor is None:
        logger.error("Failed to create event monitor - check Accessibility permissions in System Settings > Privacy & Security > Accessibility")
    else:
        logger.info("NSEvent hotkey monitor started")


def stop():
    global _monitor, _event_tap, _tap_run_loop, _tap_thread
    if _tap_run_loop is not None:
        CFRunLoopStop(_tap_run_loop)
        _tap_run_loop = None
    if _event_tap is not None:
        CFMachPortInvalidate(_event_tap)
        _event_tap = None
        logger.info("Quartz hotkey event tap stopped")
    _tap_thread = None

    if _monitor:
        NSEvent.removeMonitor_(_monitor)
        _monitor = None
        logger.info("NSEvent hotkey monitor stopped")

    for binding in _bindings:
        binding.is_active = False
    _suppressed_keycodes.clear()
