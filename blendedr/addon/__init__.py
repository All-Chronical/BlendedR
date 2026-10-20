"""BlendedR XR input-equivalence add-on."""

from . import input_actions, navigation, ui

bl_info = {
    "name": "BlendedR",
    "author": "BlendedR contributors",
    "version": (0, 1, 0),
    "blender": (5, 2, 0),
    "location": "3D View",
    "description": "XR navigation and input-equivalence experiments for Blender",
    "category": "3D View",
}


def register():
    ui.register()
    input_actions.register()
    navigation.register()


def unregister():
    navigation.unregister()
    input_actions.unregister()
    ui.unregister()
