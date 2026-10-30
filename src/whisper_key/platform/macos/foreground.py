# platform/macos/foreground.py
# Identifies the frontmost application for per-app rules, and whether a text
# input has keyboard focus (so dictation is never pasted into nothing).
# Windows mirror: platform/windows/foreground.py.
import logging

logger = logging.getLogger(__name__)


def get_foreground_app() -> dict:
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if not app:
            return {}
        return {
            'exe': (app.bundleIdentifier() or '').lower(),
            'path': str(app.bundleURL().path()) if app.bundleURL() else '',
            'title': app.localizedName() or '',
        }
    except Exception as e:
        logger.debug(f"Foreground app probe failed: {e}")
        return {}


# --- Focused text input detection -------------------------------------------
#
# Asks the Accessibility API what has keyboard focus, so a transcript isn't
# pasted into nothing. Many apps can't answer: Electron apps and Zed report no
# focused element, Chrome reports only the page. So this is three-way, and
# callers must treat None ("can't tell") as "might be a text field".

_TEXT_ROLES = {'AXTextField', 'AXTextArea', 'AXComboBox', 'AXSearchField'}
# Roles that can contain an editable field we can't see into (web content,
# custom-drawn editors), so they say nothing either way.
_OPAQUE_ROLES = {'AXWebArea', 'AXGroup', 'AXScrollArea', 'AXUnknown', 'AXLayoutArea'}


# True: a text input has focus. False: something else definitely has focus
# (a file list, a window, a button). None: the app doesn't say.
def focused_text_input():
    try:
        import ApplicationServices as AS
        system_wide = AS.AXUIElementCreateSystemWide()
        error, element = AS.AXUIElementCopyAttributeValue(system_wide, 'AXFocusedUIElement', None)
        if error != 0 or element is None:
            return None
        _, role = AS.AXUIElementCopyAttributeValue(element, 'AXRole', None)
        _, value_settable = AS.AXUIElementIsAttributeSettable(element, 'AXValue', None)
    except Exception as e:
        logger.debug(f"Focused element probe failed: {e}")
        return None

    if role in _TEXT_ROLES:
        return True
    if role in _OPAQUE_ROLES or not role:
        # An editable container (e.g. a contenteditable div) has a settable value.
        return True if value_settable else None
    return False
