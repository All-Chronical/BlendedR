"""Shared BlendedR runtime state, preferences, and navigation telemetry."""

import json
import os
from pathlib import Path
from time import monotonic

import bpy

ACTION_MAP_NAME = "blendedr_modifier_input"
GRIP_POSE_ACTION_NAME = "blendedr_controller_grip_pose"
AIM_POSE_ACTION_NAME = "blendedr_controller_aim_pose"
LEFT_HAND_PATH = "/user/hand/left"
RIGHT_HAND_PATH = "/user/hand/right"
POINTER_ACTIONS = {
    "left_b": (None, "ctrl"),
    "left_a": (None, "shift"),
    "right_b": (None, "alt"),
    "right_a": ("MIDDLEMOUSE", None),
}

pointer_buttons = set()
pointer_modifiers = set()
injected_buttons = set()
pointer_position = [512.0, 512.0]
pointer_previous_angles = None
navigation_previous_angles = None
navigation_mode = None
navigation_orbit_yaw = 0.0
navigation_orbit_pitch = 0.0
session_was_running = False

debug_state = {
    "action_events": 0,
    "last_action": "None",
    "last_mouse_event": "None",
    "mouse_events_queued": 0,
    "mouse_events_rejected": 0,
    "action_set_active": False,
    "bound_button_profiles": "None",
    "host_view": "None",
    "navigation_mode": "None",
    "navigation_log": "Disabled",
}

_navigation_log_enabled = False
_navigation_log_path = Path(__file__).resolve().parent.parent / "build" / "blendedr_navigation.jsonl"


def preferences():
    return bpy.context.preferences.addons[__package__].preferences


def _json_value(value):
    """Convert telemetry values to JSON without allowing diagnostics to interrupt input."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    try:
        return [_json_value(component) for component in value]
    except TypeError:
        return str(value)


def navigation_log_path():
    return str(_navigation_log_path)


def set_navigation_log_enabled(enabled):
    """Create or close the opt-in navigation trace immediately on preference changes."""
    global _navigation_log_enabled
    _navigation_log_enabled = enabled
    if not enabled:
        debug_state["navigation_log"] = "Disabled"
        return

    try:
        _navigation_log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"time": monotonic(), "event": "capture_started"}
        _navigation_log_path.write_text(
            json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        debug_state["navigation_log"] = f"Capturing: {_navigation_log_path.name}"
    except OSError as error:
        _navigation_log_enabled = False
        debug_state["navigation_log"] = f"Error: {error}"


def begin_navigation_log():
    """Start a fresh trace for the next XR session when capture is enabled."""
    if preferences().capture_navigation_log:
        set_navigation_log_enabled(True)


def finish_navigation_log():
    """Mark a completed trace and open it through the operating system."""
    if not _navigation_log_enabled or not _navigation_log_path.is_file():
        return

    telemetry("capture_finished")
    debug_state["navigation_log"] = f"Completed: {_navigation_log_path.name}"
    try:
        os.startfile(_navigation_log_path)  # type: ignore[attr-defined]
    except (AttributeError, OSError) as error:
        debug_state["navigation_log"] = f"Completed; open manually ({error})"


def telemetry(event, **values):
    """Append opt-in JSONL telemetry for one navigation/input observation."""
    enabled = preferences().capture_navigation_log
    if enabled != _navigation_log_enabled:
        set_navigation_log_enabled(enabled)
    if not enabled:
        return

    try:
        payload = {"time": monotonic(), "event": event}
        payload.update({key: _json_value(value) for key, value in values.items()})
        with _navigation_log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(payload, separators=(",", ":")) + "\n")
    except (OSError, TypeError, ValueError) as error:
        # Input collection must continue even if optional diagnostics fail.
        debug_state["navigation_log"] = f"Error: {error}"


def reset_input_state():
    global navigation_mode, navigation_previous_angles, pointer_previous_angles
    global navigation_orbit_yaw, navigation_orbit_pitch, session_was_running
    pointer_buttons.clear()
    pointer_modifiers.clear()
    injected_buttons.clear()
    navigation_mode = None
    navigation_previous_angles = None
    pointer_previous_angles = None
    navigation_orbit_yaw = 0.0
    navigation_orbit_pitch = 0.0
    session_was_running = False
    debug_state["navigation_mode"] = "None"
