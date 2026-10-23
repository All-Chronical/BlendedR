"""BlendedR preferences and 3D View diagnostics panel."""

import bpy
from bpy.props import BoolProperty, FloatProperty

from . import runtime


def _capture_navigation_log_update(self, _context):
    runtime.set_navigation_log_enabled(self.capture_navigation_log)


class BLENDEDR_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    disable_builtin_navigation: BoolProperty(
        name="Disable Built-in Navigation",
        description="Replace Blender's XR action set when the next XR session starts",
        default=True,
    )

    capture_navigation_log: BoolProperty(
        name="Capture Navigation Log",
        description="Write high-frequency input and XR-view telemetry to blendedr/build",
        default=False,
        update=_capture_navigation_log_update,
    )
    sync_host_view: BoolProperty(
        name="Sync Active 3D View",
        description="Reflect the active desktop 3D View's presentation settings in XR",
        default=True,
    )
    orbit_sensitivity: FloatProperty(
        name="Orbit Sensitivity",
        default=1.0,
        min=0.1,
        max=10.0,
    )
    pan_sensitivity: FloatProperty(
        name="Pan Sensitivity",
        default=1.0,
        min=0.1,
        max=10.0,
    )
    zoom_sensitivity: FloatProperty(
        name="Zoom Sensitivity",
        default=1.0,
        min=0.1,
        max=10.0,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "disable_builtin_navigation")
        layout.prop(self, "sync_host_view")
        layout.prop(self, "orbit_sensitivity")
        layout.prop(self, "pan_sensitivity")
        layout.prop(self, "zoom_sensitivity")
        layout.separator()
        layout.prop(self, "capture_navigation_log")
        layout.label(text=f"Log file: {runtime.navigation_log_path()}")
        layout.label(text="Restart the XR session after changing action-set settings.")


class VIEW3D_PT_blendedr(bpy.types.Panel):
    bl_label = "BlendedR"
    bl_idname = "VIEW3D_PT_blendedr"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "VR"

    def draw(self, context):
        preferences = runtime.preferences()
        layout = self.layout
        layout.prop(preferences, "disable_builtin_navigation")
        layout.prop(preferences, "sync_host_view")
        layout.prop(preferences, "orbit_sensitivity")
        layout.prop(preferences, "pan_sensitivity")
        layout.prop(preferences, "zoom_sensitivity")
        layout.separator()
        layout.prop(preferences, "capture_navigation_log")
        layout.label(text="Orbit: hold right A and aim-drag (yaw + pitch; global roll locked).")
        layout.label(text="Pan: hold left A with right A and aim-drag.")
        layout.label(text="Zoom: hold left B with right A and aim up/down (dollies toward the")
        layout.label(text="3D cursor, like desktop view zoom; no world scaling).")
        layout.label(text="Modifiers can be pressed before or after right A.")
        layout.label(text="Right trigger: left click; left trigger: right click.")
        layout.label(text="Right controller aim ray points the XR mouse cursor.")
        layout.separator()
        layout.label(text=f"Input actions: {'Active' if runtime.debug_state['action_set_active'] else 'Inactive'}")
        layout.label(text=f"Navigation: {runtime.debug_state['navigation_mode']}")
        layout.label(text=f"Last pointer event: {runtime.debug_state['last_mouse_event']}")
        if preferences.capture_navigation_log:
            layout.label(text=f"Telemetry: {runtime.debug_state['navigation_log']}")
            layout.label(text=f"Log: {runtime.navigation_log_path()}")


CLASSES = (
    BLENDEDR_AddonPreferences,
    VIEW3D_PT_blendedr,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
