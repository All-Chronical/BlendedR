"""World-fixed UI quad: placement and laser hit testing for the Viewport3D header in XR.

The XR draw pass shows the header region's own draw buffer, so the add-on only supplies the region
and where the quad sits. Nothing here renders or copies pixels.
"""

import bpy
from mathutils import Quaternion, Vector

from . import navigation, runtime

# A quad transform is {"location": Vector, "rotation": Quaternion, "size": (width, height)} in
# meters. The quad lies in its local XY plane, centered on its location.


def _quad_axes(quad_transform):
    rotation = Quaternion(quad_transform["rotation"])
    return (
        rotation @ Vector((1.0, 0.0, 0.0)),
        rotation @ Vector((0.0, 1.0, 0.0)),
        rotation @ Vector((0.0, 0.0, 1.0)),
    )


def ray_quad_intersect(ray_origin, ray_dir, quad_transform):
    """Return the world point where the ray hits the quad, or None.

    Pure function with no session state. Both faces count, and rays starting past the quad
    or pointing away from it miss.
    """
    x_axis, y_axis, normal = _quad_axes(quad_transform)
    center = Vector(quad_transform["location"])
    denominator = normal.dot(ray_dir)
    if abs(denominator) < 1e-9:
        return None
    distance = (center - ray_origin).dot(normal) / denominator
    if distance < 0.0:
        return None
    hit = ray_origin + distance * ray_dir
    offset = hit - center
    half_width, half_height = (dimension / 2.0 for dimension in quad_transform["size"])
    if abs(offset.dot(x_axis)) > half_width or abs(offset.dot(y_axis)) > half_height:
        return None
    return hit


def quad_point_to_pixels(hit_point, quad_transform, texture_size):
    """Map a world point on the quad to pixel (x, y) in its texture, origin bottom-left.

    The result is region-local. mouse_event_add takes window coordinates, so add the region's x
    and y before sending it. Points on or past the edge clamp to the outermost pixel.
    """
    x_axis, y_axis, _normal = _quad_axes(quad_transform)
    offset = Vector(hit_point) - Vector(quad_transform["location"])
    width, height = quad_transform["size"]
    u = offset.dot(x_axis) / width + 0.5
    v = offset.dot(y_axis) / height + 0.5
    texture_width, texture_height = texture_size
    x = min(max(int(u * texture_width), 0), texture_width - 1)
    y = min(max(int(v * texture_height), 0), texture_height - 1)
    return x, y


def _header_region(area):
    for region in area.regions:
        if region.type == "HEADER" and region.width > 0 and region.height > 0:
            return region
    return None


_NO_LOCATION = (0.0, 0.0, 0.0)
_IDENTITY_ROTATION = (1.0, 0.0, 0.0, 0.0)


def header_target():
    """The Host 3D View's header as (area, region), or None when there is no host view."""
    host = navigation._active_host_view3d()
    if host is None:
        return None
    _window, area, _space = host
    region = _header_region(area)
    if region is None:
        return None
    return area, region


def _has_quad_api(session):
    # Older Blender builds lack this RNA function; skipping keeps the pointer timer alive.
    return hasattr(session, "ui_quad_set")


def publish(session):
    """Per pointer tick: tell the XR draw pass which region to show and where. No placement yet,
    or no host view, hides the quad."""
    if not _has_quad_api(session):
        return
    target = header_target()
    transform = runtime.ui_quad_transform
    if target is None or transform is None:
        session.ui_quad_set(bpy.context, None, _NO_LOCATION, _IDENTITY_ROTATION, 0.0, 0.0)
        return
    session.ui_quad_set(
        bpy.context,
        target[1],
        transform["location"],
        transform["rotation"],
        *transform["size"],
    )


def aim_hit(aim_loc, aim_rot):
    """Where the aim ray lands on the quad: (area, region, pixel), or None when it misses."""
    transform = runtime.ui_quad_transform
    if transform is None or aim_loc is None or aim_rot is None:
        return None
    hit = ray_quad_intersect(aim_loc, aim_rot @ Vector((0.0, 0.0, -1.0)), transform)
    if hit is None:
        return None
    target = header_target()
    if target is None:
        return None
    area, region = target
    return area, region, quad_point_to_pixels(hit, transform, (region.width, region.height))


# Spawn layout: 2 m wide at 1.5 m is about 67 degrees, which puts a 1492 px header near one texture
# pixel per headset pixel. Height follows the header's aspect, so its content is never reshaped.
UI_QUAD_WIDTH = 2.0
UI_QUAD_DISTANCE = 1.5


def _viewer_pose_ready(session):
    # Until the first XR frame is drawn, the pose getters return the origin and identity.
    location = Vector(session.viewer_pose_location)
    rotation = Quaternion(session.viewer_pose_rotation)
    return location.length_squared > 0.0 or rotation != Quaternion()


def _yaw_only_rotation(rotation):
    """`rotation` with pitch and roll removed: faces the same horizontal direction, upright.

    Spawn placement must not inherit whatever the head happened to be pitched at on the first
    tick, or the quad spawns tilted to match.
    """
    forward = rotation @ Vector((0.0, 0.0, -1.0))
    forward.z = 0.0
    if forward.length_squared < 1e-8:
        # Degenerate only when looking straight up/down; arbitrary horizontal fallback.
        forward = Vector((0.0, -1.0, 0.0))
    return forward.to_track_quat("-Z", "Y")


def _spawn_transform(session, region):
    """Place the quad in front of the viewer, facing back toward them, upright and unmirrored."""
    rotation = _yaw_only_rotation(Quaternion(session.viewer_pose_rotation))
    forward = rotation @ Vector((0.0, 0.0, -1.0))
    viewer_location = Vector(session.viewer_pose_location)
    location = viewer_location + UI_QUAD_DISTANCE * forward
    aspect = region.height / region.width
    transform = {
        "location": location,
        "rotation": rotation,
        "size": (UI_QUAD_WIDTH, UI_QUAD_WIDTH * aspect),
    }
    x_axis, y_axis, normal = _quad_axes(transform)
    runtime.telemetry(
        "quad_spawn",
        location=tuple(location),
        rotation=tuple(rotation),
        size=transform["size"],
        region_size=(region.width, region.height),
        local_x_axis=tuple(x_axis),
        local_y_axis=tuple(y_axis),
        normal=tuple(normal),
        viewer_location=tuple(viewer_location),
        viewer_rotation=tuple(session.viewer_pose_rotation),
        viewer_forward=tuple(forward),
    )
    return transform


def ensure_transform(session):
    """Place the quad once per session, on the first tick with a real viewer pose."""
    if runtime.ui_quad_transform is not None or not _has_quad_api(session):
        return
    if not _viewer_pose_ready(session):
        return
    target = header_target()
    if target is None:
        return
    runtime.ui_quad_transform = _spawn_transform(session, target[1])


def unregister():
    session = bpy.context.window_manager.xr_session_state
    if session and bpy.types.XrSessionState.is_running(bpy.context) and _has_quad_api(session):
        session.ui_quad_set(bpy.context, None, _NO_LOCATION, _IDENTITY_ROTATION, 0.0, 0.0)
