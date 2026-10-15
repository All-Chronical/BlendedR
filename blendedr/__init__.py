"""BlendedR: VR controls for Blender."""

from . import spacedrag

bl_info = {
    "name": "BlendedR",
    "author": "BlendedR contributors",
    "version": (0, 1, 0),
    "blender": (5, 2, 0),
    "location": "3D View",
    "description": "Control Blender in virtual reality",
    "category": "3D View",
}


def register():
    """Register addon components with Blender."""
    spacedrag.register()


def unregister():
    """Unregister addon components with Blender."""
    spacedrag.unregister()
