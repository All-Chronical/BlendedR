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
ZOOM_MIN_DISTANCE = 0.1
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
            is_array = getattr(property_definition, "is_array", False)
            setattr(target, identifier, tuple(value) if is_array else value)
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
        ("show_cursor", "show_cursor"),
        ("show_object_origins", "show_object_origins"),
        ("show_motion_paths", "show_motion_paths"),
        ("show_relationship_lines", "show_relationship_lines"),
    ):
        if hasattr(overlay, overlay_property):
            setattr(session_settings, session_property, getattr(overlay, overlay_property))

    runtime.debug_state["host_view"] = f"{window.as_pointer():x}:{area.as_pointer():x}"


def _right_aim_angles(session):
    """Return the right-controller aim as screen angles in the viewer (head) frame.

    Desktop middle-mouse dragging is screen-relative: horizontal motion orbits and vertical
    motion pitches regardless of how the view is oriented in the world. Measuring the aim in
    the viewer frame reproduces that mapping in XR. The previous world-frame measurement
    coupled and stretched the yaw/pitch split whenever the aim ray pointed steeply up/down or
    the head was tilted, which was perceived as roll while orbiting.

    Both poses are published with the same base and navigation transforms applied
    (wm_xr_session_controller_pose_calc / wm_xr_session_state_update), so un-applying the
    viewer rotation also removes navigation; no explicit navigation un-apply is needed.
    """
    world_rotation = Quaternion(session.controller_aim_rotation_get(bpy.context, 1))
    viewer_rotation = Quaternion(session.viewer_pose_rotation)
    rotation = viewer_rotation.inverted() @ world_rotation
    direction = rotation @ Vector((0.0, 0.0, -1.0))
    direction.normalize()
    # Viewer frame: -Z forward, +X screen right, +Y screen up.
    return atan2(direction.x, -direction.z), asin(max(-1.0, min(1.0, direction.y)))


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
    if runtime.navigation_mode == "ORBIT":
        # Orbit is applied as a camera-space rotation derived from the live viewer pose, not a
        # world-frame closed form. The XR camera transform is viewer_pose = navigation * base *
        # head, so a world-frame turntable (yaw about world Z, pitch about world X) sandwiches the
        # head's yaw between the pitch axis and the camera: the camera-right axis lifts off the
        # horizon by sin(head yaw) * sin(pitch), which is perceived as scene roll in the HMD.
        # Desktop keeps the horizon because its pitch axis is the camera's own right axis,
        # leveled to the horizon (view3d_navigate_view_rotate.cc / ndof horizon lock), not a
        # world axis. Mirror that here.
        chain = Quaternion(session.viewer_pose_rotation)
        forward = chain @ Vector((0.0, 0.0, -1.0))
        right = chain @ Vector((1.0, 0.0, 0.0))
        up = chain @ Vector((0.0, 1.0, 0.0))

        # Horizon leveling: rotate about the view axis until the world-up direction lies in the
        # screen's vertical plane (camera up aligned with the projection of world Z). This is
        # self-healing (removes roll inherited from older states) and a no-op while already level.
        world_up_screen = Vector((0.0, 0.0, 1.0)) - forward * forward.z
        level_angle = 0.0
        if world_up_screen.length_squared > 1e-8:
            world_up_screen.normalize()
            level_angle = atan2((up.cross(world_up_screen)).dot(forward), up.dot(world_up_screen))
            level = Quaternion(forward, level_angle)
            right = level @ right
            up = level @ up

        # Pitch axis: the camera's right axis, which is horizontal after leveling (cross of the
        # view axis with world Z up projected on screen is always horizontal).
        pitch_axis = right.copy()
        if pitch_axis.length_squared < 1e-8:
            pitch_axis = Vector((1.0, 0.0, 0.0))
        pitch_axis.normalize()

        yaw_angle = -yaw_delta * preferences.orbit_sensitivity
        pitch_angle = -pitch_delta * preferences.orbit_sensitivity
        # Desktop turntable orbit cannot pitch through the poles. Clamp the resulting camera
        # elevation before applying the pitch delta; otherwise repeated aim-drag can invert the
        # viewer and make horizon leveling ambiguous near the pole.
        forward_after_pitch = Matrix.Rotation(pitch_angle, 4, pitch_axis).to_3x3() @ forward
        current_elevation = asin(max(-1.0, min(1.0, forward.z)))
        next_elevation = asin(max(-1.0, min(1.0, forward_after_pitch.z)))
        elevation_limit = ORBIT_PITCH_LIMIT - 0.001
        if abs(next_elevation) > elevation_limit:
            pitch_angle = (max(-elevation_limit, min(elevation_limit, next_elevation)) - current_elevation)
        # Upside-down flip (desktop viewrotate "reverse"): inverted camera inverts the
        # turntable yaw response.
        if up.z < 0.0:
            yaw_angle = -yaw_angle

        # Camera delta: level, then pitch about the (already horizontal) camera right axis,
        # then yaw about world Z. Applied to the camera chain, this keeps the camera right
        # axis on the horizon by construction, for any head orientation.
        delta = (
            Matrix.Rotation(yaw_angle, 4, "Z")
            @ Matrix.Rotation(pitch_angle, 4, pitch_axis)
            @ Matrix.Rotation(level_angle, 4, forward)
        ).to_quaternion()
        session.navigation_rotation = delta @ Quaternion(session.navigation_rotation)
        # Exact orbit around the anchor: the navigation transform as a whole is rotated about
        # the anchor (nav_location_new = anchor + R * (nav_location_old - anchor)). Deriving the
        # shift from the viewer pose instead leaves a per-tick error of (I - R) * (nav - viewer)
        # whenever the head is displaced from the navigation origin, which accumulates as drift.
        anchor = Vector(bpy.context.scene.cursor.location)
        session.navigation_location = anchor + delta @ (
            Vector(session.navigation_location) - anchor
        )
    elif runtime.navigation_mode == "PAN":
        viewer_rotation = Quaternion(session.viewer_pose_rotation)
        camera_right = viewer_rotation @ Vector((1.0, 0.0, 0.0))
        camera_up = viewer_rotation @ Vector((0.0, 1.0, 0.0))
        anchor = Vector(bpy.context.scene.cursor.location)
        distance = max((Vector(session.viewer_pose_location) - anchor).length, 0.25)
        pan_delta = (
            -camera_right * yaw_delta * distance * preferences.pan_sensitivity
            + camera_up * pitch_delta * distance * preferences.pan_sensitivity
        )
        session.navigation_location = Vector(session.navigation_location) + pan_delta
    elif runtime.navigation_mode == "ZOOM":
        # Desktop-equivalent zoom: dolly toward the orbit anchor (3D Cursor). Relative (log)
        # speed in both directions; navigation scale stays untouched, so screen-sized elements
        # such as cameras and lights change apparent size exactly like the scene does.
        anchor = Vector(bpy.context.scene.cursor.location)
        offset = Vector(session.viewer_pose_location) - anchor
        distance = max(offset.length, ZOOM_MIN_DISTANCE)
        factor = exp(-pitch_delta * preferences.zoom_sensitivity)
        factor = max(distance * factor, ZOOM_MIN_DISTANCE) / distance
        session.navigation_location = Vector(session.navigation_location) + offset * (factor - 1.0)


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
        runtime.pointer_previous_world_rotation = None
        runtime.debug_state["navigation_mode"] = "None"
        return POINTER_TIMER_INTERVAL

    if preferences.sync_host_view:
        _sync_host_view_presentation()

    _poll_action_states(session)

    # Stale-pose gate: controller poses are republished once per XR frame with the navigation
    # transform active at that time. If a timer tick runs before a republish, the stored pose
    # predates the latest navigation write, and un-applying the *current* navigation rotation
    # would inject a spurious delta (perceived as wobble/roll while orbiting). An unchanged
    # world-space aim rotation means either no new frame or a truly still controller; in both
    # cases skipping the delta is exactly equivalent to applying a zero delta.
    aim_world_rotation = tuple(session.controller_aim_rotation_get(bpy.context, 1))
    aim_is_fresh = aim_world_rotation != runtime.pointer_previous_world_rotation

    aim_angles = _right_aim_angles(session)
    if aim_is_fresh:
        if runtime.pointer_previous_angles is not None:
            pointer_delta = _angles_delta(runtime.pointer_previous_angles, aim_angles)
            if pointer_delta is not None:
                yaw_delta, pitch_delta = pointer_delta
                runtime.pointer_position[0] += yaw_delta * POINTER_PIXELS_PER_RADIAN
                runtime.pointer_position[1] += pitch_delta * POINTER_PIXELS_PER_RADIAN
        runtime.pointer_previous_angles = aim_angles
        runtime.pointer_previous_world_rotation = aim_world_rotation

    desired = "MIDDLEMOUSE" in runtime.pointer_buttons
    requested_mode = _navigation_mode_from_modifiers()
    if desired and runtime.navigation_mode != requested_mode:
        previous_mode = runtime.navigation_mode
        runtime.navigation_mode = requested_mode
        # Seed from the aim pose only when it is fresh; otherwise defer seeding to the first
        # fresh tick so a pose predating the last navigation write cannot cause a jump.
        runtime.navigation_previous_angles = aim_angles if aim_is_fresh else None
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
        before = _session_snapshot(session, "before")
        navigation_delta = None
        if aim_is_fresh:
            if runtime.navigation_previous_angles is None:
                # Deferred seed (drag began on a stale pose): establish the baseline now.
                runtime.navigation_previous_angles = aim_angles
            else:
                navigation_delta = _angles_delta(runtime.navigation_previous_angles, aim_angles)
            runtime.navigation_previous_angles = aim_angles
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
