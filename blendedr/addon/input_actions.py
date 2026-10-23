"""OpenXR action registration and controller-button state collection."""

import bpy
from bpy.app.handlers import persistent

from . import runtime

# These profiles match Blender's built-in VR Scene Inspection action map.
CONTROLLER_PROFILES = (
    ("huawei", "/interaction_profiles/huawei/controller"),
    ("index", "/interaction_profiles/valve/index_controller"),
    ("oculus", "/interaction_profiles/oculus/touch_controller"),
    ("reverb_g2", "/interaction_profiles/hp/mixed_reality_controller"),
    ("simple", "/interaction_profiles/khr/simple_controller"),
    ("vive", "/interaction_profiles/htc/vive_controller"),
    ("vive_cosmos", "/interaction_profiles/htc/vive_cosmos_controller"),
    ("vive_focus", "/interaction_profiles/htc/vive_focus3_controller"),
    ("wmr", "/interaction_profiles/microsoft/motion_controller"),
)
OPTIONAL_PROFILE_SETTINGS = {
    "huawei": "vr_actions_enable_huawei",
    "reverb_g2": "vr_actions_enable_reverb_g2",
    "vive_cosmos": "vr_actions_enable_vive_cosmos",
    "vive_focus": "vr_actions_enable_vive_focus",
}
# A/B/Y/X have the expected face-button semantics only on Oculus Touch. Pose actions remain
# available for every Blender-supported controller profile, but no guessed face-button mappings
# are registered for other hardware.
MODIFIER_PROFILE_PATHS = {
    "oculus": ("/input/y/click", "/input/b/click", "/input/x/click", "/input/a/click"),
}


def _profile_is_enabled(binding_name):
    setting_name = OPTIONAL_PROFILE_SETTINGS.get(binding_name)
    return setting_name is None or getattr(bpy.context.scene, setting_name, False)


def _add_pose_action(action_map, name, component_path, is_grip, is_aim):
    action = action_map.actionmap_items.new(name, True)
    action.type = "POSE"
    action.pose_is_controller_grip = is_grip
    action.pose_is_controller_aim = is_aim
    action.user_paths.new(runtime.LEFT_HAND_PATH)
    action.user_paths.new(runtime.RIGHT_HAND_PATH)

    for binding_name, profile in CONTROLLER_PROFILES:
        if not _profile_is_enabled(binding_name):
            continue
        binding = action.bindings.new(binding_name, True)
        binding.profile = profile
        binding.component_paths.new(component_path)
        binding.component_paths.new(component_path)


def _add_modifier_actions(action_map):
    # Each action has exactly one OpenXR subaction path. Binding a logical left-hand action to
    # both hands caused right-hand events to invoke and then be discarded by `modal()`, multiplying
    # the event stream and creating duplicate modal handlers.
    action_bindings = {
        "left_b": (runtime.LEFT_HAND_PATH, 0),
        "left_a": (runtime.LEFT_HAND_PATH, 2),
        "right_b": (runtime.RIGHT_HAND_PATH, 1),
        "right_a": (runtime.RIGHT_HAND_PATH, 3),
    }

    for action_name, (hand_path, component_index) in action_bindings.items():
        action = action_map.actionmap_items.new(f"blendedr_{action_name}", True)
        # Face-button `/click` paths are digital OpenXR inputs. BOOLEAN prevents threshold
        # chatter from the FLOAT action path from leaving modifier state stuck.
        action.type = "BOOLEAN"
        action.user_paths.new(hand_path)
        action.op = "blendedr.pointer_input"
        action.op_mode = "MODAL"

        for binding_name, profile in CONTROLLER_PROFILES:
            if binding_name not in MODIFIER_PROFILE_PATHS or not _profile_is_enabled(binding_name):
                continue
            binding = action.bindings.new(binding_name, True)
            binding.profile = profile
            binding.component_paths.new(MODIFIER_PROFILE_PATHS[binding_name][component_index])

    for action_name, hand_path in (
        ("right_trigger", runtime.RIGHT_HAND_PATH),
        ("left_trigger", runtime.LEFT_HAND_PATH),
    ):
        action = action_map.actionmap_items.new(f"blendedr_{action_name}", True)
        action.type = "FLOAT"
        action.user_paths.new(hand_path)
        action.op = "blendedr.pointer_input"
        action.op_mode = "MODAL"
        for binding_name, profile in CONTROLLER_PROFILES:
            if binding_name == "simple" or not _profile_is_enabled(binding_name):
                continue
            binding = action.bindings.new(binding_name, True)
            binding.profile = profile
            binding.component_paths.new("/input/trigger/value")
            binding.threshold = 0.5


class BLENDEDR_OT_pointer_input(bpy.types.Operator):
    """Collect controller button state for the navigation adapter."""

    bl_idname = "blendedr.pointer_input"
    bl_label = "BlendedR XR Pointer Input"

    def invoke(self, context, event):
        self.action_name = event.xr.action if event.type == "XR_ACTION" and event.xr else ""
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "XR_ACTION" or not event.xr:
            return {"PASS_THROUGH"}

        if event.xr.action != self.action_name:
            return {"PASS_THROUGH"}

        action_name = event.xr.action.removeprefix("blendedr_")
        mapping = runtime.POINTER_ACTIONS.get(action_name)
        if mapping is None:
            return {"PASS_THROUGH"}

        expected_hand = (
            runtime.LEFT_HAND_PATH if action_name.startswith("left_") else runtime.RIGHT_HAND_PATH
        )
        if event.xr.user_path != expected_hand:
            runtime.telemetry(
                "action_ignored",
                action=action_name,
                value=event.value,
                user_path=event.xr.user_path,
                expected_user_path=expected_hand,
            )
            return {"RUNNING_MODAL"}

        button, modifier = mapping
        if event.value == "PRESS":
            if modifier:
                runtime.pointer_modifiers.add(modifier)
            if button:
                runtime.pointer_buttons.add(button)
        elif event.value == "RELEASE":
            if modifier:
                runtime.pointer_modifiers.discard(modifier)
            if button:
                runtime.pointer_buttons.discard(button)

        runtime.debug_state["action_events"] += 1
        runtime.debug_state["last_action"] = f"{action_name}: {event.value}"
        runtime.telemetry(
            "action",
            action=action_name,
            value=event.value,
            user_path=event.xr.user_path,
            button=button,
            modifier=modifier,
            pointer_buttons=sorted(runtime.pointer_buttons),
            pointer_modifiers=sorted(runtime.pointer_modifiers),
        )
        return {"RUNNING_MODAL"}


def _input_setup_failed(message):
    runtime.telemetry("input_setup", active=False, error=message)
    print(f"[BlendedR] {message}")


@persistent
def create_modifier_actions(_unused=None):
    context = bpy.context
    runtime.reset_input_state()
    runtime.begin_navigation_log()
    runtime.debug_state["action_set_active"] = False
    preferences = runtime.preferences()
    if not preferences.disable_builtin_navigation:
        runtime.telemetry("input_setup", active=False, error="built-in navigation replacement disabled")
        return

    session_state = context.window_manager.xr_session_state
    if not session_state:
        _input_setup_failed("XR session state is unavailable; input actions were not created.")
        return

    action_map = session_state.actionmaps.new(session_state, runtime.ACTION_MAP_NAME, True)
    if not action_map:
        _input_setup_failed("Could not create the XR input action map.")
        return

    _add_pose_action(
        action_map,
        runtime.GRIP_POSE_ACTION_NAME,
        "/input/grip/pose",
        is_grip=True,
        is_aim=False,
    )
    _add_pose_action(
        action_map,
        runtime.AIM_POSE_ACTION_NAME,
        "/input/aim/pose",
        is_grip=False,
        is_aim=True,
    )
    _add_modifier_actions(action_map)

    if not session_state.action_set_create(context, action_map):
        _input_setup_failed("Could not create the XR input action set.")
        return

    for action in action_map.actionmap_items:
        if not session_state.action_create(context, action_map, action):
            _input_setup_failed(f"Could not create XR action: {action.name}")
            return
        for binding in action.bindings:
            if not session_state.action_binding_create(context, action_map, action, binding):
                _input_setup_failed(
                    f"Could not bind XR action {action.name} for profile {binding.profile}."
                )
                return

    if not session_state.controller_pose_actions_set(
        context,
        runtime.ACTION_MAP_NAME,
        runtime.GRIP_POSE_ACTION_NAME,
        runtime.AIM_POSE_ACTION_NAME,
    ):
        _input_setup_failed("Could not configure controller pose actions.")
        return
    if not session_state.active_action_set_set(context, runtime.ACTION_MAP_NAME):
        _input_setup_failed("Could not activate the XR input action set.")
        return

    runtime.debug_state["action_set_active"] = True
    runtime.debug_state["bound_button_profiles"] = ", ".join(MODIFIER_PROFILE_PATHS)
    runtime.telemetry(
        "input_setup",
        active=True,
        action_names=[action.name for action in action_map.actionmap_items],
        trigger_profiles=[
            profile_name
            for profile_name, _profile in CONTROLLER_PROFILES
            if profile_name != "simple" and _profile_is_enabled(profile_name)
        ],
    )


def register():
    bpy.utils.register_class(BLENDEDR_OT_pointer_input)
    handlers = bpy.app.handlers.xr_session_start_pre
    if create_modifier_actions not in handlers:
        handlers.append(create_modifier_actions)


def unregister():
    handlers = bpy.app.handlers.xr_session_start_pre
    if create_modifier_actions in handlers:
        handlers.remove(create_modifier_actions)
    bpy.utils.unregister_class(BLENDEDR_OT_pointer_input)
