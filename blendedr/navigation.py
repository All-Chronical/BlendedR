"""Grip-based navigation (Orbit, Spacedrag, and Zoom) for OpenXR."""

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, FloatProperty
from mathutils import Quaternion, Vector

ACTION_MAP_NAME = "blendedr_navigation"
POSE_ACTION_NAME = "blendedr_controller_grip_pose"
AIM_ACTION_NAME = "blendedr_controller_aim_pose"
GRAB_ACTION_NAME = "blendedr_grab"
LEFT_HAND_PATH = "/user/hand/left"
RIGHT_HAND_PATH = "/user/hand/right"

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
    action.user_paths.new(LEFT_HAND_PATH)
    action.user_paths.new(RIGHT_HAND_PATH)

    for binding_name, profile, _ in CONTROLLER_PROFILES:
        binding = action.bindings.new(binding_name, True)
        binding.profile = profile
        binding.component_paths.new(component_path)
        binding.component_paths.new(component_path)


def _add_grab_action(action_map):
    action = action_map.actionmap_items.new(GRAB_ACTION_NAME, True)
    action.type = "FLOAT"
    action.user_paths.new(LEFT_HAND_PATH)
    action.user_paths.new(RIGHT_HAND_PATH)
    action.op = "blendedr.navigation_grab"
    action.op_mode = "MODAL"
    action.bimanual = True

    for binding_name, profile, component_path in CONTROLLER_PROFILES:
        binding = action.bindings.new(binding_name, True)
        binding.profile = profile
        binding.threshold = 0.5
        binding.component_paths.new(component_path)
        binding.component_paths.new(component_path)


class BLENDEDR_OT_navigation_grab(bpy.types.Operator):
    bl_idname = "blendedr.navigation_grab"
    bl_label = "BlendedR Navigation Grab"

    def invoke(self, context, event):
        self.previous_bimanual = event.xr.bimanual
        self.previous_location = Vector(event.xr.controller_location)
        self.previous_location_other = Vector(event.xr.controller_location_other)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "XR_ACTION" or event.xr.action != GRAB_ACTION_NAME:
            return {"PASS_THROUGH"}

        preferences = context.preferences.addons[__package__].preferences
        navigation = context.window_manager.xr_session_state
        is_bimanual = event.xr.bimanual
        current_location = Vector(event.xr.controller_location)
        current_location_other = Vector(event.xr.controller_location_other)

        if is_bimanual != self.previous_bimanual:
            self.previous_bimanual = is_bimanual
            self.previous_location = current_location
            self.previous_location_other = current_location_other
        elif is_bimanual:
            previous_distance = (self.previous_location - self.previous_location_other).length
            current_distance = (current_location - current_location_other).length
            if previous_distance:
                scale_factor = (previous_distance / current_distance) ** preferences.zoom_sensitivity
                navigation.navigation_scale *= scale_factor
            self.previous_location = current_location
            self.previous_location_other = current_location_other
        elif event.xr.user_path == LEFT_HAND_PATH:
            delta = current_location - self.previous_location
            self._orbit(context, navigation, delta, preferences.spacedrag_sensitivity)
            self.previous_location = current_location
        elif event.xr.user_path == RIGHT_HAND_PATH:
            delta = current_location - self.previous_location
            navigation.navigation_location = (
                Vector(navigation.navigation_location)
                - delta * preferences.spacedrag_sensitivity
            )
            self.previous_location = current_location

        if event.value == "RELEASE":
            return {"FINISHED"}
        return {"RUNNING_MODAL"}

    @staticmethod
    def _orbit(context, navigation, hand_delta, sensitivity):
        viewer_rotation = Quaternion(navigation.viewer_pose_rotation)
        camera_right = viewer_rotation @ Vector((1.0, 0.0, 0.0))
        camera_up = viewer_rotation @ Vector((0.0, 1.0, 0.0))
        horizontal = hand_delta.dot(camera_right)
        vertical = hand_delta.dot(camera_up)

        orbit_rotation = (
            Quaternion(camera_up, -horizontal * sensitivity)
            @ Quaternion(camera_right, -vertical * sensitivity)
        )
        pivot = Vector(context.space_data.region_3d.view_location)
        viewer_location = Vector(navigation.viewer_pose_location)
        rotated_viewer_location = pivot + orbit_rotation @ (viewer_location - pivot)

        navigation.navigation_location = (
            Vector(navigation.navigation_location)
            + rotated_viewer_location
            - viewer_location
        )
        navigation.navigation_rotation = (
            orbit_rotation @ Quaternion(navigation.navigation_rotation)
        )


class BLENDEDR_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    spacedrag_enabled: BoolProperty(
        name="Spacedrag",
        description="Enable right-hand grip to pan the VR view",
        default=True,
    )
    spacedrag_sensitivity: FloatProperty(
        name="Drag Amount",
        description="Scale the controller movement used for Orbit and Spacedrag",
        default=1.0,
        min=1.0,
        max=20.0,
        precision=2,
    )
    zoom_sensitivity: FloatProperty(
        name="Zoom Amount",
        description="Scale the zoom response to changes in hand separation",
        default=1.0,
        min=1.0,
        max=20.0,
        precision=2,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "spacedrag_enabled")
        row = layout.row()
        row.enabled = self.spacedrag_enabled
        row.prop(self, "spacedrag_sensitivity", slider=True)
        row = layout.row()
        row.enabled = self.spacedrag_enabled
        row.prop(self, "zoom_sensitivity", slider=True)


class VIEW3D_PT_blendedr(bpy.types.Panel):
    bl_label = "BlendedR"
    bl_idname = "VIEW3D_PT_blendedr"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "VR"

    def draw(self, context):
        preferences = context.preferences.addons[__package__].preferences
        layout = self.layout
        layout.prop(preferences, "spacedrag_enabled")
        row = layout.row()
        row.enabled = preferences.spacedrag_enabled
        row.prop(preferences, "spacedrag_sensitivity", slider=True)
        row = layout.row()
        row.enabled = preferences.spacedrag_enabled
        row.prop(preferences, "zoom_sensitivity", slider=True)


@persistent
def create_navigation_actions(context):
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
    _add_grab_action(action_map)

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


CLASSES = (
    BLENDEDR_OT_navigation_grab,
    BLENDEDR_AddonPreferences,
    VIEW3D_PT_blendedr,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)

    if bpy.app.build_options.xr_openxr:
        handlers = bpy.app.handlers.xr_session_start_pre
        if create_navigation_actions not in handlers:
            handlers.append(create_navigation_actions)


def unregister():
    handlers = bpy.app.handlers.xr_session_start_pre
    if create_navigation_actions in handlers:
        handlers.remove(create_navigation_actions)

    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
