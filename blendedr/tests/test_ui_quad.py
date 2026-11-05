"""Pure-math tests for the UI quad's laser hit and pixel mapping (no XR session needed).

Run with ``blendedr/tests/run_tests.cmd``.
"""

import sys
import types
import unittest
from pathlib import Path

from mathutils import Quaternion, Vector

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import addon  # noqa: E402,F401
import test_xr_pointer as pointer_tests  # noqa: E402
from addon import navigation, runtime, ui_quad  # noqa: E402

TEXTURE_SIZE = (1024, 512)
# A 1 m x 0.5 m quad two meters in front of a viewer at the origin who looks down -Z.
QUAD = {
    "location": Vector((0.0, 0.0, -2.0)),
    "rotation": Quaternion((1.0, 0.0, 0.0, 0.0)),
    "size": (1.0, 0.5),
}
EYE = Vector((0.0, 0.0, 0.0))
FORWARD = Vector((0.0, 0.0, -1.0))


def aim_at(x, y):
    """Unit direction from the eye toward the point (x, y) on the quad's plane."""
    return Vector((x, y, -2.0)).normalized()


class RayQuadIntersect(unittest.TestCase):
    def test_center_hit(self):
        hit = ui_quad.ray_quad_intersect(EYE, FORWARD, QUAD)
        self.assertIsNotNone(hit)
        self.assertAlmostEqual((hit - Vector((0.0, 0.0, -2.0))).length, 0.0, places=6)

    def test_parallel_ray_misses(self):
        self.assertIsNone(ui_quad.ray_quad_intersect(EYE, Vector((1.0, 0.0, 0.0)), QUAD))

    def test_quad_behind_viewer_misses(self):
        origin = Vector((0.0, 0.0, -3.0))
        self.assertIsNone(ui_quad.ray_quad_intersect(origin, FORWARD, QUAD))

    def test_ray_outside_quad_misses(self):
        self.assertIsNone(ui_quad.ray_quad_intersect(EYE, aim_at(0.6, 0.0), QUAD))

    def test_ray_inside_edge_hits(self):
        hit = ui_quad.ray_quad_intersect(EYE, aim_at(0.499, -0.249), QUAD)
        self.assertIsNotNone(hit)


class QuadPointToPixels(unittest.TestCase):
    def pixels(self, x, y):
        return ui_quad.quad_point_to_pixels(
            Vector((x, y, -2.0)), QUAD, TEXTURE_SIZE
        )

    def test_center_maps_to_texture_center(self):
        self.assertEqual(self.pixels(0.0, 0.0), (512, 256))

    def test_bottom_left_corner_maps_to_origin(self):
        self.assertEqual(self.pixels(-0.5, -0.25), (0, 0))

    def test_top_right_edge_clamps_into_bounds(self):
        self.assertEqual(self.pixels(0.5, 0.25), (1023, 511))

    def test_point_past_edge_clamps_into_bounds(self):
        self.assertEqual(self.pixels(9.0, -9.0), (1023, 0))

    def test_top_is_high_y(self):
        self.assertGreater(self.pixels(0.0, 0.2)[1], self.pixels(0.0, -0.2)[1])


class HitToPixels(unittest.TestCase):
    def test_ray_hit_maps_to_expected_pixel(self):
        hit = ui_quad.ray_quad_intersect(EYE, aim_at(0.25, 0.125), QUAD)
        self.assertIsNotNone(hit)
        x, y = ui_quad.quad_point_to_pixels(hit, QUAD, TEXTURE_SIZE)
        self.assertEqual((x, y), (768, 384))


class RecordingSession(pointer_tests.FakeSession):
    """FakeSession that keeps each event's target area, so quad and scene events can be told apart."""

    def __init__(self):
        super().__init__()
        self.sent = []

    def mouse_event_add(self, _context, event_type, value, **kwargs):
        self.sent.append((event_type, value, kwargs.get("area"), kwargs.get("x"), kwargs.get("y")))
        return True


# The header at an arbitrary spot in the window, sized like TEXTURE_SIZE.
REGION = types.SimpleNamespace(x=2, y=986, width=1024, height=512)


class QuadPressLock(unittest.TestCase):
    """Drives the real _pointer_timer: a press is locked to the target it started on (Step 6)."""

    HEADER = ("HEADER_AREA", REGION)

    def setUp(self):
        self.session = RecordingSession()
        self.saved = (navigation.bpy, runtime.bpy)
        navigation.bpy = runtime.bpy = pointer_tests.fake_bpy(self.session)
        self.saved_target = ui_quad.header_target
        ui_quad.header_target = lambda: self.HEADER
        self.saved_popups = ui_quad._popup_regions
        ui_quad._popup_regions = lambda: []
        runtime.reset_input_state()
        runtime.debug_state["action_set_active"] = True
        runtime.ui_quad_transform = QUAD
        self.session.aim_loc = Vector((0.0, 0.0, 0.0))
        self.aim_at(0.0)

    def tearDown(self):
        navigation.bpy, runtime.bpy = self.saved
        ui_quad.header_target = self.saved_target
        ui_quad._popup_regions = self.saved_popups
        runtime.reset_input_state()

    def aim_at(self, x):
        """Aim from the origin at the point (x, 0, -2): x=0 is the quad's center."""
        self.session.aim_rot = Vector((0.0, 0.0, -1.0)).rotation_difference(Vector((x, 0.0, -2.0)))

    def tick(self, n=1, **action_values):
        self.session.action_values.update(action_values)
        for _ in range(n):
            navigation._pointer_timer()

    def sent_to(self, target, event_type=None):
        return [
            e
            for e in self.session.sent
            if e[2] == target and (event_type is None or e[0] == event_type)
        ]

    def test_hover_goes_to_header_only(self):
        self.tick(1)
        moves = self.sent_to("HEADER_AREA", "MOUSEMOVE")
        self.assertTrue(moves)
        self.assertEqual(moves[-1][3:], (REGION.x + 512, REGION.y + 256))
        self.assertEqual(self.sent_to(None, "MOUSEMOVE"), [])

    def test_leaving_the_quad_moves_the_pointer_off_the_header_once(self):
        self.tick(1)
        self.aim_at(0.9)
        self.tick(3)
        moves = self.sent_to("HEADER_AREA", "MOUSEMOVE")
        self.assertEqual(moves[-1][3:], (REGION.x - 1, REGION.y - 1))
        self.assertEqual(len(moves), 2)

    def test_press_on_quad_releases_on_quad_after_aim_leaves(self):
        self.tick(1, right_trigger=1.0)
        self.assertEqual([e[1] for e in self.sent_to("HEADER_AREA", "LEFTMOUSE")], ["PRESS"])
        self.aim_at(0.9)
        self.tick(1, right_trigger=1.0)
        self.assertEqual(self.sent_to(None, "LEFTMOUSE"), [])
        self.tick(1, right_trigger=0.0)
        releases = self.sent_to("HEADER_AREA", "LEFTMOUSE")
        self.assertEqual([e[1] for e in releases], ["PRESS", "RELEASE"])
        self.assertEqual(releases[-1][3:], (REGION.x + 512, REGION.y + 256))
        self.assertEqual(self.sent_to(None, "LEFTMOUSE"), [])
        self.assertEqual(runtime.ui_injected_buttons, set())

    def test_scene_press_blocks_quad_press_until_release(self):
        self.aim_at(0.9)
        self.tick(1, right_trigger=1.0)
        self.assertEqual([e[1] for e in self.sent_to(None, "LEFTMOUSE")], ["PRESS"])
        self.aim_at(0.0)
        self.tick(1, right_trigger=1.0)
        self.assertEqual(self.sent_to("HEADER_AREA", "LEFTMOUSE"), [])
        self.tick(1, right_trigger=0.0)
        self.assertEqual([e[1] for e in self.sent_to(None, "LEFTMOUSE")], ["PRESS", "RELEASE"])
        self.assertEqual(self.sent_to("HEADER_AREA", "LEFTMOUSE"), [])

    def test_quad_press_blocks_scene_press_of_another_button(self):
        self.tick(1, right_trigger=1.0)
        self.aim_at(0.9)
        self.tick(1, right_trigger=1.0, left_trigger=1.0)
        self.assertEqual(self.sent_to(None, "RIGHTMOUSE"), [])
        self.assertEqual(self.sent_to(None, "LEFTMOUSE"), [])


class RecordingQuadSession:
    def __init__(self, location=(0.0, 0.0, 0.0), rotation=(1.0, 0.0, 0.0, 0.0)):
        self.viewer_pose_location = location
        self.viewer_pose_rotation = rotation
        self.quad_calls = []

    def ui_quads_clear(self, _context):
        self.quad_calls.clear()

    def ui_quad_add(self, _context, region, location, rotation, width, height):
        self.quad_calls.append((region, tuple(location), tuple(rotation), width, height))


# A menu below the header's middle, in the same window as REGION. REGION's quad is 1 m per 1024 px.
POPUP = types.SimpleNamespace(x=512, y=186, width=256, height=256)
SUBMENU = types.SimpleNamespace(x=768, y=186, width=128, height=128)


class Publish(unittest.TestCase):
    HEADER = ("HEADER_AREA", REGION)

    def setUp(self):
        self.saved_target = ui_quad.header_target
        ui_quad.header_target = lambda: self.HEADER
        self.saved_popups = ui_quad._popup_regions
        self.popups = []
        ui_quad._popup_regions = lambda: self.popups
        self.saved_telemetry = runtime.telemetry
        self.logged = []
        runtime.telemetry = lambda event, **values: self.logged.append((event, values))

    def tearDown(self):
        ui_quad.header_target = self.saved_target
        ui_quad._popup_regions = self.saved_popups
        runtime.telemetry = self.saved_telemetry
        runtime.ui_quad_transform = None
        runtime.ui_popup_rects = ()

    def test_logs_popups_only_when_they_change(self):
        session = RecordingQuadSession()
        runtime.ui_quad_transform = QUAD
        self.popups = [POPUP]
        ui_quad.publish(session)
        ui_quad.publish(session)
        self.popups = []
        ui_quad.publish(session)
        self.assertEqual(
            self.logged,
            [("quad_popups", {"rects": ((512, 186, 256, 256),)}), ("quad_popups", {"rects": ()})],
        )

    def test_publishes_the_header_region_and_placement(self):
        session = RecordingQuadSession()
        runtime.ui_quad_transform = QUAD
        ui_quad.publish(session)
        self.assertEqual(
            session.quad_calls, [(REGION, (0.0, 0.0, -2.0), (1.0, 0.0, 0.0, 0.0), 1.0, 0.5)]
        )

    def test_publishes_nothing_until_placed(self):
        session = RecordingQuadSession()
        session.quad_calls.append("stale")
        ui_quad.publish(session)
        self.assertEqual(session.quad_calls, [])

    def test_publishes_open_popups_after_the_header(self):
        session = RecordingQuadSession()
        runtime.ui_quad_transform = QUAD
        self.popups = [POPUP, SUBMENU]
        ui_quad.publish(session)
        self.assertEqual([c[0] for c in session.quad_calls], [REGION, POPUP, SUBMENU])

    def test_older_build_without_quad_api_is_skipped(self):
        ui_quad.publish(pointer_tests.FakeSession())  # must not raise


class PopupLayout(unittest.TestCase):
    def transform(self, popup=POPUP, level=1):
        return ui_quad._popup_transform(QUAD, REGION, popup, level)

    def test_keeps_the_header_pixel_scale(self):
        self.assertEqual(self.transform()["size"], (0.25, 0.25))

    def test_sits_where_it_does_relative_to_the_header_in_the_window(self):
        location = self.transform()["location"]
        # Popup center (640, 314) against header center (514, 1242), at 1 m per 1024 px.
        self.assertAlmostEqual(location.x, 126 / 1024, 5)
        self.assertAlmostEqual(location.y, -928 / 1024, 5)

    def test_each_level_sits_closer_to_the_viewer(self):
        self.assertAlmostEqual(self.transform()["location"].z, -2.0 + ui_quad.UI_POPUP_LIFT, 5)
        self.assertGreater(
            self.transform(level=2)["location"].z, self.transform(level=1)["location"].z
        )


class PopupAim(unittest.TestCase):
    HEADER = ("HEADER_AREA", REGION)

    def setUp(self):
        self.saved_target = ui_quad.header_target
        ui_quad.header_target = lambda: self.HEADER
        self.saved_popups = ui_quad._popup_regions
        self.popups = [POPUP]
        ui_quad._popup_regions = lambda: self.popups
        runtime.ui_quad_transform = QUAD

    def tearDown(self):
        ui_quad.header_target = self.saved_target
        ui_quad._popup_regions = self.saved_popups
        runtime.ui_quad_transform = None

    def aim(self, x, y, z=-2.0):
        rotation = FORWARD.rotation_difference(Vector((x, y, z)))
        return ui_quad.aim_hit(EYE, rotation)

    def popup_center(self):
        return ui_quad._popup_transform(QUAD, REGION, POPUP, 1)["location"]

    def test_hit_on_a_popup_reports_its_window_position(self):
        center = self.popup_center()
        area, region, xy = self.aim(center.x, center.y, center.z)
        self.assertEqual((area, region), self.HEADER)
        self.assertEqual(xy, (POPUP.x + 128, POPUP.y + 128))

    def test_header_stays_reachable_behind_an_open_popup(self):
        self.assertEqual(self.aim(0.0, 0.0)[2], (REGION.x + 512, REGION.y + 256))

    def test_open_popup_keeps_tracking_past_every_quad(self):
        _area, _region, xy = self.aim(1.0, 0.0)
        self.assertEqual(xy, (REGION.x + 1536, REGION.y + 256))

    def test_no_popup_means_a_miss_past_the_quad(self):
        self.popups = []
        self.assertIsNone(self.aim(1.0, 0.0))

    def test_ray_away_from_the_plane_misses_even_with_a_popup(self):
        self.assertIsNone(self.aim(0.0, 0.0, 1.0))


def fake_view3d(*regions):
    space = types.SimpleNamespace(mirror_xr_session=False)
    return types.SimpleNamespace(
        type="VIEW_3D",
        spaces=types.SimpleNamespace(active=space),
        regions=[types.SimpleNamespace(type="HEADER", width=w, height=h) for w, h in regions],
    )


class HostLookup(unittest.TestCase):
    def setUp(self):
        self.saved = navigation.bpy

    def tearDown(self):
        navigation.bpy = self.saved

    def use_context(self, context_area, screen_area):
        window = types.SimpleNamespace(screen=types.SimpleNamespace(areas=[screen_area]))
        navigation.bpy = types.SimpleNamespace(
            context=types.SimpleNamespace(
                area=context_area,
                window=window,
                window_manager=types.SimpleNamespace(windows=[window]),
            )
        )

    def test_ignores_the_xr_offscreen_area_in_the_context(self):
        offscreen = fake_view3d((0, 0))
        host = fake_view3d((1492, 32))
        self.use_context(offscreen, host)
        self.assertIs(ui_quad.header_target()[0], host)

    def test_header_with_no_size_is_no_target(self):
        self.use_context(None, fake_view3d((0, 0)))
        self.assertIsNone(ui_quad.header_target())


class Spawn(unittest.TestCase):
    """Placement against the pose a real session reported at spawn time."""

    VIEWER_LOCATION = (7.321, -6.879, 4.906)
    VIEWER_ROTATION = (0.7385, 0.5719, 0.2365, 0.2675)
    HEADER = ("HEADER_AREA", types.SimpleNamespace(x=2, y=986, width=1492, height=32))

    def setUp(self):
        self.saved_target = ui_quad.header_target
        ui_quad.header_target = lambda: self.HEADER
        self.saved_telemetry = runtime.telemetry
        runtime.telemetry = lambda *_args, **_kwargs: None
        runtime.ui_quad_transform = None

    def tearDown(self):
        ui_quad.header_target = self.saved_target
        runtime.telemetry = self.saved_telemetry
        runtime.ui_quad_transform = None

    def spawn(self):
        ui_quad.ensure_transform(
            RecordingQuadSession(self.VIEWER_LOCATION, self.VIEWER_ROTATION)
        )
        return runtime.ui_quad_transform

    def test_waits_for_the_first_real_viewer_pose(self):
        ui_quad.ensure_transform(RecordingQuadSession())
        self.assertIsNone(runtime.ui_quad_transform)
        self.assertIsNotNone(self.spawn())

    def test_sits_in_front_of_the_viewer_at_eye_height(self):
        transform = self.spawn()
        viewer = Vector(self.VIEWER_LOCATION)
        self.assertAlmostEqual((transform["location"] - viewer).length, ui_quad.UI_QUAD_DISTANCE, 5)
        self.assertAlmostEqual(transform["location"].z, viewer.z, 5)

    def test_viewer_sees_the_front_face_unmirrored(self):
        transform = self.spawn()
        x_axis, y_axis, normal = ui_quad._quad_axes(transform)
        to_viewer = Vector(self.VIEWER_LOCATION) - transform["location"]
        self.assertGreater(normal.dot(to_viewer), 0.0)
        viewer_right = (-to_viewer).cross(Vector((0.0, 0.0, 1.0)))
        self.assertGreater(x_axis.dot(viewer_right), 0.0)
        self.assertGreater(y_axis.z, 0.99)

    def test_keeps_the_header_aspect(self):
        width, height = self.spawn()["size"]
        self.assertAlmostEqual(width / height, 1492 / 32, 5)


if __name__ == "__main__":
    unittest.main()
