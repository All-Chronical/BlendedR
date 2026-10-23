"""Host View3D presentation sync and authoritative XR navigation."""

from math import asin, atan2, exp, pi

import bpy
from mathutils import Matrix, Quaternion, Vector

from . import runtime

POINTER_TIMER_INTERVAL = 1.0 / 90.0
# The pick view Blender builds for button events is centered on the right controller's aim ray,
# so the virtual pointer is always the center pixel of the 1024x1024 pick grid.
POINTER_GRID_CENTER = 512
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


def _base_pose_rotation():
    """Return the orientation of Blender's XR base pose in world space."""
    settings = bpy.context.window_manager.xr_session_settings
    if settings.base_pose_type == "CUSTOM":
        base_yaw = settings.base_pose_angle
    else:
        base_object = (
            settings.base_pose_object
            if settings.base_pose_type == "OBJECT"
            else bpy.context.scene.camera
        )
        base_yaw = (
            base_object.matrix_world.to_quaternion().to_euler().z if base_object else 0.0
        )
    return (
        Matrix.Rotation(base_yaw, 4, "Z") @ Matrix.Rotation(pi / 2.0, 4, "X")
    ).to_quaternion()


def _right_aim_angles(session, reference_rotation=None):
    """Return controller aim angles relative to a reference that excludes the HMD.

    Blender publishes controller aim in world space as navigation @ base @ raw_controller.
    For orbit, reference_rotation is navigation @ base: its inverse removes those transforms
    and leaves the controller orientation in the basket frame. The HMD pose is not part of this
    calculation. If no reference is supplied, retain the viewer-relative measurement used by the
    virtual pointer and the existing pan/zoom behavior.
    """
    world_rotation = Quaternion(session.controller_aim_rotation_get(bpy.context, 1))
    reference = (
        Quaternion(session.viewer_pose_rotation)
        if reference_rotation is None
        else Quaternion(reference_rotation)
    )
    rotation = reference.inverted() @ world_rotation
    direction = rotation @ Vector((0.0, 0.0, -1.0))
    direction.normalize()
    return atan2(direction.x, -direction.z), asin(max(-1.0, min(1.0, direction.y)))


def _laser_ray_info(session):
    """Telemetry-only prediction of what the laser touches.

    Blender picks along the right controller's aim ray (the pick view is centered on it, and the
    pointer is the grid's center pixel), so a ray cast from the same pose predicts the pick result.
    Comparing the two in the capture shows whether a miss is a pose/projection problem or an
    object the select engine cannot see.
    """
    settings = bpy.context.window_manager.xr_session_settings
    far = max(1.0, settings.clip_end * 0.9)
    origin = Vector(session.controller_aim_location_get(bpy.context, 1))
    direction = Quaternion(session.controller_aim_rotation_get(bpy.context, 1)) @ Vector(
        (0.0, 0.0, -1.0)
    )
    direction.normalize()

    hit_object = None
    location = None
    distance = far
    try:
        hit, location, _normal, _index, hit_object, _matrix = bpy.context.scene.ray_cast(
            bpy.context.evaluated_depsgraph_get(), origin, direction, distance=far
        )
    except (RuntimeError, TypeError, ValueError):
        hit = False
    if hit and location is not None:
        distance = (location - origin).length
    else:
        hit_object = None
    return {
        "ray_hit": bool(hit),
        "ray_object": hit_object.name if hit_object else None,
        "ray_distance": distance,
        "ray_origin": tuple(origin),
        "ray_direction": tuple(direction),
    }


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
        # The basket orientation is navigation @ base. Do not read viewer_pose_rotation here:
        # that also includes the HMD observer, whose orientation must not steer orbit.
        basket_rotation = (
            Quaternion(session.navigation_rotation) @ _base_pose_rotation()
        )
        forward = basket_rotation @ Vector((0.0, 0.0, -1.0))
        pitch_axis = forward.cross(Vector((0.0, 0.0, 1.0)))
        if pitch_axis.length_squared < 1e-8:
            pitch_axis = Vector((1.0, 0.0, 0.0))
        else:
            pitch_axis.normalize()

        yaw_angle = -yaw_delta * preferences.orbit_sensitivity
        pitch_angle = -pitch_delta * preferences.orbit_sensitivity
        # Keep the basket's view direction short of the vertical poles. Since pitch is applied
        # around the horizontal basket-right axis, this clamp is independent of HMD orientation.
        forward_after_pitch = Matrix.Rotation(pitch_angle, 4, pitch_axis).to_3x3() @ forward
        current_elevation = asin(max(-1.0, min(1.0, forward.z)))
        next_elevation = asin(max(-1.0, min(1.0, forward_after_pitch.z)))
        elevation_limit = ORBIT_PITCH_LIMIT - 0.001
        if abs(next_elevation) > elevation_limit:
            pitch_angle = (
                max(-elevation_limit, min(elevation_limit, next_elevation)) - current_elevation
            )

        # Yaw is about world up. Pitch is about the horizontal right axis of the basket view.
        # No leveling feedback is needed: the HMD observer is excluded from these axes.
        delta = (
            Matrix.Rotation(yaw_angle, 4, "Z")
            @ Matrix.Rotation(pitch_angle, 4, pitch_axis)
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
    pointer_kwargs = {"x": POINTER_GRID_CENTER, "y": POINTER_GRID_CENTER}
    for button in tuple(runtime.injected_buttons):
        _inject_mouse_event(session, button, "RELEASE", **pointer_kwargs)
    runtime.injected_buttons.clear()


def _inject_mouse_event(session, event_type, event_value, **kwargs):
    """Queue an XR offscreen-viewport mouse event and keep transport observable."""
    if not hasattr(session, "mouse_event_add"):
        runtime.debug_state["last_mouse_event"] = "mouse_event_add is unavailable in this Blender build"
        runtime.debug_state["mouse_events_rejected"] += 1
        return False

    try:
        queued = session.mouse_event_add(bpy.context, event_type, event_value, **kwargs)
        error = None
    except (RuntimeError, TypeError, ValueError) as exception:
        queued = False
        error = str(exception)

    runtime.debug_state["last_mouse_event"] = (
        f"{event_type}: {event_value} ({'queued' if queued else 'rejected'})"
    )
    if queued:
        runtime.debug_state["mouse_events_queued"] += 1
    else:
        runtime.debug_state["mouse_events_rejected"] += 1

    if event_type != "MOUSEMOVE":
        runtime.telemetry(
            "native_mouse_event",
            type=event_type,
            value=event_value,
            queued=queued,
            x=kwargs.get("x"),
            y=kwargs.get("y"),
            pointer_buttons=sorted(runtime.pointer_buttons),
            error=error,
            **runtime.pointer_target_info,
        )
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
        runtime.pointer_previous_world_rotation = None
        runtime.debug_state["navigation_mode"] = "None"
        return POINTER_TIMER_INTERVAL

    if preferences.sync_host_view:
        _sync_host_view_presentation()

    # The fork's XR draw path renders the right controller aim ray in the headset view.
    settings = bpy.context.window_manager.xr_session_settings
    if settings.controller_draw_style not in {"DARK_RAY", "LIGHT_RAY"}:
        settings.controller_draw_style = "LIGHT_RAY"

    _poll_action_states(session)

    # Stale-pose gate: controller poses are republished once per XR frame with the navigation
    # transform active at that time. If a timer tick runs before a republish, the stored pose
    # predates the latest navigation write, and un-applying the *current* navigation rotation
    # would inject a spurious delta (perceived as wobble/roll while orbiting). An unchanged
    # world-space aim rotation means either no new frame or a truly still controller; in both
    # cases skipping the delta is exactly equivalent to applying a zero delta.
    aim_world_rotation = tuple(session.controller_aim_rotation_get(bpy.context, 1))
    aim_is_fresh = aim_world_rotation != runtime.pointer_previous_world_rotation

    pointer_angles = _right_aim_angles(session)
    requested_mode = _navigation_mode_from_modifiers()
    if requested_mode == "ORBIT":
        basket_rotation = Quaternion(session.navigation_rotation) @ _base_pose_rotation()
        navigation_angles = _right_aim_angles(session, basket_rotation)
    else:
        navigation_angles = pointer_angles

    if aim_is_fresh and not runtime.injected_buttons:
        # Ray-cast telemetry is frozen while buttons are held so it describes the clicked target.
        runtime.pointer_target_info = _laser_ray_info(session)
        runtime.pointer_previous_world_rotation = aim_world_rotation

    desired = "MIDDLEMOUSE" in runtime.pointer_buttons
    if desired and runtime.navigation_mode != requested_mode:
        previous_mode = runtime.navigation_mode
        runtime.navigation_mode = requested_mode
        # Seed from the aim pose only when it is fresh; otherwise defer seeding to the first
        # fresh tick so a pose predating the last navigation write cannot cause a jump.
        runtime.navigation_previous_angles = navigation_angles if aim_is_fresh else None
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
            aim_angles=navigation_angles,
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
                runtime.navigation_previous_angles = navigation_angles
            else:
                navigation_delta = _angles_delta(
                    runtime.navigation_previous_angles, navigation_angles
                )
            runtime.navigation_previous_angles = navigation_angles
            if navigation_delta is not None:
                _apply_xr_navigation(session, *navigation_delta, preferences)
        runtime.telemetry(
            "navigation_tick",
            mode=runtime.navigation_mode,
            aim_angles=navigation_angles,
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
            aim_angles=navigation_angles,
            **_session_snapshot(session, "before"),
        )
        runtime.navigation_mode = None
        runtime.navigation_previous_angles = None
        runtime.debug_state["navigation_mode"] = "None"

    pointer_kwargs = {
        "x": POINTER_GRID_CENTER,
        "y": POINTER_GRID_CENTER,
        "ctrl": "ctrl" in runtime.pointer_modifiers,
        "shift": "shift" in runtime.pointer_modifiers,
        "alt": "alt" in runtime.pointer_modifiers,
    }
    _inject_mouse_event(session, "MOUSEMOVE", "NOTHING", **pointer_kwargs)

    desired_buttons = set(runtime.pointer_buttons)
    for button in ("MIDDLEMOUSE", "LEFTMOUSE", "RIGHTMOUSE"):
        desired_button = button in desired_buttons
        injected = button in runtime.injected_buttons
        if desired_button != injected and _inject_mouse_event(
            session,
            button,
            "PRESS" if desired_button else "RELEASE",
            **pointer_kwargs,
        ):
            if desired_button:
                runtime.injected_buttons.add(button)
            else:
                runtime.injected_buttons.discard(button)
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
