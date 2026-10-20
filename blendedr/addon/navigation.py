"""Host View3D presentation sync and authoritative XR navigation."""

from math import asin, atan2, exp, pi

import bpy
from mathutils import Matrix, Quaternion, Vector

from . import runtime

POINTER_TIMER_INTERVAL = 1.0 / 90.0
POINTER_PIXELS_PER_RADIAN = 1000.0
NAVIGATION_DEADZONE_RADIANS = 0.001
NAVIGATION_MAX_DELTA_RADIANS = 0.15
ORBIT_PITCH_LIMIT = pi / 2.0
ACTION_STATE_THRESHOLD = 0.5


def _active_host_view3d():
    context_area = bpy.context.area
    if context_area and context_area.type == "VIEW_3D":
        space = context_area.spaces.active
        if not space.mirror_xr_session:
            return bpy.context.window, context_area, space

    active_window = bpy.context.window
    windows = [active_window] if active_window else []
    windows.extend(
        window for window in bpy.context.window_manager.windows if window != active_window
    )
    for window in windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            space = area.spaces.active
            if not space.mirror_xr_session:
                return window, area, space
    return None


def _copy_writable_rna_properties(source, target):
    for property_definition in source.bl_rna.properties:
        identifier = property_definition.identifier
        if identifier == "rna_type" or property_definition.is_readonly:
            continue
        try:
            value = getattr(source, identifier)
            setattr(target, identifier, tuple(value) if property_definition.is_array else value)
        except (AttributeError, TypeError, ValueError):
            continue


def _sync_host_view_presentation():
    host = _active_host_view3d()
    if host is None:
        runtime.debug_state["host_view"] = "None"
        return

    window, area, view3d = host
    session_settings = bpy.context.window_manager.xr_session_settings
    _copy_writable_rna_properties(view3d.shading, session_settings.shading)

    overlay = view3d.overlay
    for session_property, overlay_property in (
        ("show_floor", "show_floor"),
        ("show_object_extras", "show_extras"),
        ("show_selection", "show_outline_selected"),
    ):
        if hasattr(overlay, overlay_property):
            setattr(session_settings, session_property, getattr(overlay, overlay_property))

    runtime.debug_state["host_view"] = f"{window.as_pointer():x}:{area.as_pointer():x}"


def _right_aim_angles(session):
    """Return right-controller aim in navigation-local, not world, space.

    Blender publishes controller poses after applying `navigation_rotation`. Removing that
    transform prevents an orbit update from being reinterpreted as controller motion on the next
    timer tick.
    """
    world_rotation = Quaternion(session.controller_aim_rotation_get(bpy.context, 1))
    navigation_rotation = Quaternion(session.navigation_rotation)
    rotation = navigation_rotation.inverted() @ world_rotation
    direction = rotation @ Vector((0.0, 0.0, -1.0))
    direction.normalize()
    return atan2(direction.x, -direction.y), asin(max(-1.0, min(1.0, direction.z)))


def _angles_delta(previous_angles, current_angles):
    previous_yaw, previous_pitch = previous_angles
    yaw, pitch = current_angles
    yaw_delta = (yaw - previous_yaw + pi) % (2.0 * pi) - pi
    pitch_delta = pitch - previous_pitch
    if max(abs(yaw_delta), abs(pitch_delta)) > NAVIGATION_MAX_DELTA_RADIANS:
        return None
    if abs(yaw_delta) < NAVIGATION_DEADZONE_RADIANS:
        yaw_delta = 0.0
    if abs(pitch_delta) < NAVIGATION_DEADZONE_RADIANS:
        pitch_delta = 0.0
    return yaw_delta, pitch_delta


def _navigation_mode_from_modifiers():
    if "ctrl" in runtime.pointer_modifiers:
        return "ZOOM"
    if "shift" in runtime.pointer_modifiers:
        return "PAN"
    return "ORBIT"


def _poll_action_states(session):
    """Rebuild input state from the authoritative OpenXR action states.

    Modal XR events can drop RELEASE transitions, which permanently sticks modifiers (observed
    as a stuck Shift after releasing left A). Polling every timer tick self-corrects within one
    interval; returns False (leaving event-derived state untouched) if polling is unavailable.
    """
    state_get = getattr(session, "action_state_get", None)
    if state_get is None or not runtime.debug_state["action_set_active"]:
        return False

    buttons = set()
    modifiers = set()
    states = {}
    for action_name, (button, modifier) in runtime.POINTER_ACTIONS.items():
        hand = (
            runtime.LEFT_HAND_PATH if action_name.startswith("left_") else runtime.RIGHT_HAND_PATH
        )
        try:
            state = state_get(
                bpy.context, runtime.ACTION_MAP_NAME, f"blendedr_{action_name}", hand
            )
            value = float(state[0])
        except (RuntimeError, TypeError, ValueError, IndexError):
            return False
        states[action_name] = value
        if value > ACTION_STATE_THRESHOLD:
            if button:
                buttons.add(button)
            if modifier:
                modifiers.add(modifier)

    if buttons != runtime.pointer_buttons or modifiers != runtime.pointer_modifiers:
        runtime.telemetry(
            "state_poll",
            previous_buttons=sorted(runtime.pointer_buttons),
            buttons=sorted(buttons),
            previous_modifiers=sorted(runtime.pointer_modifiers),
            modifiers=sorted(modifiers),
            states=states,
        )
    runtime.pointer_buttons.clear()
    runtime.pointer_buttons.update(buttons)
    runtime.pointer_modifiers.clear()
    runtime.pointer_modifiers.update(modifiers)
    return True


def _turntable_angles(navigation_rotation):
    """Extract global yaw/pitch from a navigation rotation, discarding any roll drift."""
    matrix = Quaternion(navigation_rotation).to_matrix()
    yaw = atan2(matrix[1][0], matrix[0][0])
    pitch = atan2(matrix[2][1], matrix[2][2])
    return yaw, pitch


def _turntable_rotation(yaw, pitch):
    """Global turntable rotation: yaw about world Z, then pitch about world X.

    Recomposing from only these two angles guarantees the navigation transform can never
    accumulate roll, no matter how yaw and pitch deltas are interleaved.
    """
    return (Matrix.Rotation(yaw, 4, "Z") @ Matrix.Rotation(pitch, 4, "X")).to_quaternion()


def _session_snapshot(session, prefix):
    return {
        f"{prefix}_viewer_location": tuple(session.viewer_pose_location),
        f"{prefix}_viewer_rotation": tuple(session.viewer_pose_rotation),
        f"{prefix}_navigation_location": tuple(session.navigation_location),
        f"{prefix}_navigation_rotation": tuple(session.navigation_rotation),
        f"{prefix}_navigation_scale": session.navigation_scale,
    }


def _apply_xr_navigation(session, yaw_delta, pitch_delta, preferences):
    viewer_rotation = Quaternion(session.viewer_pose_rotation)
    camera_right = viewer_rotation @ Vector((1.0, 0.0, 0.0))
    camera_up = viewer_rotation @ Vector((0.0, 1.0, 0.0))

    if runtime.navigation_mode == "ORBIT":
        # Keep navigation in a global yaw/pitch representation. Rebuilding the quaternion from
        # those two global axes each tick prevents successive updates from accumulating roll.
        runtime.navigation_orbit_yaw -= yaw_delta * preferences.orbit_sensitivity
        runtime.navigation_orbit_pitch = max(
            -ORBIT_PITCH_LIMIT + 0.001,
            min(
                ORBIT_PITCH_LIMIT - 0.001,
                runtime.navigation_orbit_pitch - pitch_delta * preferences.orbit_sensitivity,
            ),
        )
        navigation_rotation = _turntable_rotation(
            runtime.navigation_orbit_yaw, runtime.navigation_orbit_pitch
        )
        orbit_rotation = navigation_rotation @ Quaternion(session.navigation_rotation).inverted()
        anchor = Vector(bpy.context.scene.cursor.location)
        viewer_location = Vector(session.viewer_pose_location)
        rotated_viewer_location = anchor + orbit_rotation @ (viewer_location - anchor)
        session.navigation_location = (
            Vector(session.navigation_location) + rotated_viewer_location - viewer_location
        )
        session.navigation_rotation = navigation_rotation
    elif runtime.navigation_mode == "PAN":
        anchor = Vector(bpy.context.scene.cursor.location)
        distance = max((Vector(session.viewer_pose_location) - anchor).length, 0.25)
        pan_delta = (
            -camera_right * yaw_delta * distance * preferences.pan_sensitivity
            + camera_up * pitch_delta * distance * preferences.pan_sensitivity
        )
        session.navigation_location = Vector(session.navigation_location) + pan_delta
    elif runtime.navigation_mode == "ZOOM":
        session.navigation_scale *= exp(-pitch_delta * preferences.zoom_sensitivity)


def _release_injected_buttons(session):
    pointer_kwargs = {
        "x": round(runtime.pointer_position[0]),
        "y": round(runtime.pointer_position[1]),
    }
    for button in tuple(runtime.injected_buttons):
        _inject_mouse_event(session, button, "RELEASE", **pointer_kwargs)
    runtime.injected_buttons.clear()


def _inject_mouse_event(session, event_type, event_value, **kwargs):
    """Keep the experimental native-event transport observable but non-authoritative."""
    if not hasattr(session, "mouse_event_add"):
        runtime.debug_state["last_mouse_event"] = "mouse_event_add is unavailable in this Blender build"
        runtime.debug_state["mouse_events_rejected"] += 1
        return False

    queued = session.mouse_event_add(bpy.context, event_type, event_value, **kwargs)
    runtime.debug_state["last_mouse_event"] = (
        f"{event_type}: {event_value} ({'queued' if queued else 'rejected'})"
    )
    if queued:
        runtime.debug_state["mouse_events_queued"] += 1
    else:
        runtime.debug_state["mouse_events_rejected"] += 1

    runtime.telemetry(
        "native_mouse_event",
        type=event_type,
        value=event_value,
        queued=queued,
        x=kwargs.get("x"),
        y=kwargs.get("y"),
        ctrl=kwargs.get("ctrl", False),
        shift=kwargs.get("shift", False),
        alt=kwargs.get("alt", False),
    )
    if runtime.preferences().debug_input:
        print(f"[BlendedR] Mouse event: {runtime.debug_state['last_mouse_event']}")
    return queued


def _pointer_timer():
    preferences = runtime.preferences()
    session = bpy.context.window_manager.xr_session_state
    is_running = session and bpy.types.XrSessionState.is_running(bpy.context)
    if is_running:
        runtime.session_was_running = True
    elif runtime.session_was_running:
        runtime.finish_navigation_log()
        runtime.session_was_running = False

    if not preferences.disable_builtin_navigation or not is_running:
        if is_running:
            _release_injected_buttons(session)
        else:
            runtime.injected_buttons.clear()
        runtime.navigation_mode = None
        runtime.navigation_previous_angles = None
        runtime.pointer_previous_angles = None
        runtime.debug_state["navigation_mode"] = "None"
        return POINTER_TIMER_INTERVAL

    if preferences.sync_host_view:
        _sync_host_view_presentation()

    _poll_action_states(session)

    aim_angles = _right_aim_angles(session)
    if runtime.pointer_previous_angles is not None:
        pointer_delta = _angles_delta(runtime.pointer_previous_angles, aim_angles)
        if pointer_delta is not None:
            yaw_delta, pitch_delta = pointer_delta
            runtime.pointer_position[0] += yaw_delta * POINTER_PIXELS_PER_RADIAN
            runtime.pointer_position[1] += pitch_delta * POINTER_PIXELS_PER_RADIAN
    runtime.pointer_previous_angles = aim_angles

    desired = "MIDDLEMOUSE" in runtime.pointer_buttons
    requested_mode = _navigation_mode_from_modifiers()
    if desired and runtime.navigation_mode != requested_mode:
        previous_mode = runtime.navigation_mode
        runtime.navigation_mode = requested_mode
        runtime.navigation_previous_angles = aim_angles
        # Re-seed the turntable state from the live navigation rotation so a gesture (or an
        # add-on reload mid-session) never snaps the view or inherits stale angles.
        runtime.navigation_orbit_yaw, runtime.navigation_orbit_pitch = _turntable_angles(
            session.navigation_rotation
        )
        runtime.debug_state["navigation_mode"] = runtime.navigation_mode.title()
        telemetry_event = "navigation_begin" if previous_mode is None else "navigation_mode_change"
        runtime.telemetry(
            telemetry_event,
            mode=runtime.navigation_mode,
            previous_mode=previous_mode,
            aim_angles=aim_angles,
            pointer_buttons=sorted(runtime.pointer_buttons),
            pointer_modifiers=sorted(runtime.pointer_modifiers),
            **_session_snapshot(session, "before"),
        )
    elif desired:
        navigation_delta = _angles_delta(runtime.navigation_previous_angles, aim_angles)
        runtime.navigation_previous_angles = aim_angles
        before = _session_snapshot(session, "before")
        if navigation_delta is not None:
            _apply_xr_navigation(session, *navigation_delta, preferences)
        runtime.telemetry(
            "navigation_tick",
            mode=runtime.navigation_mode,
            aim_angles=aim_angles,
            yaw_delta=None if navigation_delta is None else navigation_delta[0],
            pitch_delta=None if navigation_delta is None else navigation_delta[1],
            delta_accepted=navigation_delta is not None,
            **before,
            **_session_snapshot(session, "after"),
        )
    elif runtime.navigation_mode is not None:
        runtime.telemetry(
            "navigation_end",
            mode=runtime.navigation_mode,
            aim_angles=aim_angles,
            **_session_snapshot(session, "before"),
        )
        runtime.navigation_mode = None
        runtime.navigation_previous_angles = None
        runtime.debug_state["navigation_mode"] = "None"

    pointer_kwargs = {
        "x": round(runtime.pointer_position[0]),
        "y": round(runtime.pointer_position[1]),
        "ctrl": "ctrl" in runtime.pointer_modifiers,
        "shift": "shift" in runtime.pointer_modifiers,
        "alt": "alt" in runtime.pointer_modifiers,
    }
    if desired:
        _inject_mouse_event(session, "MOUSEMOVE", "NOTHING", **pointer_kwargs)

    injected = "MIDDLEMOUSE" in runtime.injected_buttons
    if desired != injected and _inject_mouse_event(
        session,
        "MIDDLEMOUSE",
        "PRESS" if desired else "RELEASE",
        **pointer_kwargs,
    ):
        if desired:
            runtime.injected_buttons.add("MIDDLEMOUSE")
        else:
            runtime.injected_buttons.discard("MIDDLEMOUSE")
    return POINTER_TIMER_INTERVAL


def register():
    if not bpy.app.build_options.xr_openxr:
        return
    if not bpy.app.timers.is_registered(_pointer_timer):
        bpy.app.timers.register(
            _pointer_timer,
            first_interval=POINTER_TIMER_INTERVAL,
            persistent=True,
        )


def unregister():
    session = bpy.context.window_manager.xr_session_state
    if session and bpy.types.XrSessionState.is_running(bpy.context):
        _release_injected_buttons(session)
    runtime.reset_input_state()
    if bpy.app.timers.is_registered(_pointer_timer):
        bpy.app.timers.unregister(_pointer_timer)
