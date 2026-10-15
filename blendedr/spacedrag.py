"""Right-hand Spacedrag action for Blender's OpenXR session."""

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty

ACTION_MAP_NAME = "blendedr_spacedrag"
POSE_ACTION_NAME = "blendedr_controller_grip_pose"
AIM_ACTION_NAME = "blendedr_controller_aim_pose"
SPACEDRAG_ACTION_NAME = "blendedr_spacedrag"

# These match the controller interaction profiles and squeeze paths used by
# Blender 5.2's built-in VR Scene Inspection action map.
CONTROLLER_PROFILES = (
    ("huawei", "/interaction_profiles/huawei/controller", "/input/trackpad/click"),
    ("index", "/interaction_profiles/valve/index_controller", "/input/squeeze/force"),
    ("oculus", "/interaction_profiles/oculus/touch_controller", "/input/squeeze/value"),
    ("reverb_g2", "/interaction_profiles/hp/mixed_reality_controller", "/input/squeeze/value"),
    ("simple", "/interaction_profiles/khr/simple_controller", "/input/menu/click"),
    ("vive", "/interaction_profiles/htc/vive_controller", "/input/squeeze/click"),
    ("vive_cosmos", "/interaction_profiles/htc/vive_cosmos_controller", "/input/squeeze/click"),
    ("vive_focus", "/interaction_profiles/htc/vive_focus3_controller", "/input/squeeze/click"),
    ("wmr", "/interaction_profiles/microsoft/motion_controller", "/input/squeeze/click"),
)


def _add_pose_action(action_map, name, component_path, is_grip, is_aim):
    action = action_map.actionmap_items.new(name, True)
    action.type = "POSE"
    action.pose_is_controller_grip = is_grip
    action.pose_is_controller_aim = is_aim
    action.user_paths.new("/user/hand/right")

    for binding_name, profile, _ in CONTROLLER_PROFILES:
        binding = action.bindings.new(binding_name, True)
        binding.profile = profile
        binding.component_paths.new(component_path)


def _add_spacedrag_action(action_map):
    action = action_map.actionmap_items.new(SPACEDRAG_ACTION_NAME, True)
    action.type = "FLOAT"
    action.user_paths.new("/user/hand/right")
    action.op = "wm.xr_navigation_grab"
    action.op_mode = "MODAL"
    action.bimanual = False
    action.op_properties.lock_rotation = True

    for binding_name, profile, component_path in CONTROLLER_PROFILES:
        binding = action.bindings.new(binding_name, True)
        binding.profile = profile
        binding.threshold = 0.5
        binding.component_paths.new(component_path)


class BLENDEDR_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    spacedrag_enabled: BoolProperty(
        name="Spacedrag",
        description="Enable right-hand grip to pan the VR view",
        default=True,
    )

    def draw(self, context):
        self.layout.prop(self, "spacedrag_enabled")


class VIEW3D_PT_blendedr(bpy.types.Panel):
    bl_label = "BlendedR"
    bl_idname = "VIEW3D_PT_blendedr"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "VR"

    def draw(self, context):
        preferences = context.preferences.addons[__package__].preferences
        self.layout.prop(preferences, "spacedrag_enabled")


@persistent
def create_spacedrag_actions(context):
    preferences = bpy.context.preferences.addons[__package__].preferences
    if not preferences.spacedrag_enabled:
        return

    session_state = context.window_manager.xr_session_state
    if not session_state:
        return

    action_map = session_state.actionmaps.new(session_state, ACTION_MAP_NAME, True)
    if not action_map:
        return

    _add_pose_action(
        action_map,
        POSE_ACTION_NAME,
        "/input/grip/pose",
        is_grip=True,
        is_aim=False,
    )
    _add_pose_action(
        action_map,
        AIM_ACTION_NAME,
        "/input/aim/pose",
        is_grip=False,
        is_aim=True,
    )
    _add_spacedrag_action(action_map)

    if not session_state.action_set_create(context, action_map):
        return

    for action in action_map.actionmap_items:
        if not session_state.action_create(context, action_map, action):
            return
        for binding in action.bindings:
            session_state.action_binding_create(
                context, action_map, action, binding
            )

    session_state.controller_pose_actions_set(
        context, ACTION_MAP_NAME, POSE_ACTION_NAME, AIM_ACTION_NAME
    )
    session_state.active_action_set_set(context, ACTION_MAP_NAME)


CLASSES = (BLENDEDR_AddonPreferences, VIEW3D_PT_blendedr)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)

    if bpy.app.build_options.xr_openxr:
        handlers = bpy.app.handlers.xr_session_start_pre
        if create_spacedrag_actions not in handlers:
            handlers.append(create_spacedrag_actions)


def unregister():
    handlers = bpy.app.handlers.xr_session_start_pre
    if create_spacedrag_actions in handlers:
        handlers.remove(create_spacedrag_actions)

    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
