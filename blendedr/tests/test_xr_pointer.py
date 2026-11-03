"""Headless logic/dataflow tests for the BlendedR XR pointer pipeline.

Run with ``blendedr/tests/run_tests.cmd`` (uses the freshly built Blender, no headset needed).
Covers the add-on side of the pipeline and cross-checks it against the C++ constants and
Blender's real camera projection. The C++ event dispatch itself needs a live XR session; it is
covered by the flight recorder (blendedr_xr_trace.jsonl) + analyze_xr_interaction.py.
"""

import re
import sys
import types
import unittest
from math import radians, tan
from pathlib import Path

import bpy
from mathutils import Euler, Quaternion, Vector

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))

import addon  # noqa: E402,F401
from addon import navigation, runtime  # noqa: E402

INTERN_HH = REPO / "source/blender/windowmanager/xr/intern/wm_xr_intern.hh"
AIM_ROT = Euler((radians(90), 0.0, 0.0)).to_quaternion()  # aim along +Y


def cpp_constant(name):
    text = INTERN_HH.read_text(encoding="utf-8")
    match = re.search(rf"{name}\s*=\s*([0-9.]+)f?\s*;", text)
    assert match, f"{name} not found in {INTERN_HH}"
    return float(match.group(1))


class ConstantsMatchCpp(unittest.TestCase):
    def test_half_angle(self):
        self.assertAlmostEqual(
            navigation.POINTER_PICK_HALF_ANGLE, cpp_constant("XR_POINTER_PICK_HALF_ANGLE")
        )

    def test_grid_center(self):
        self.assertEqual(
            navigation.POINTER_GRID_CENTER * 2, int(cpp_constant("XR_POINTER_REGION_SIZE"))
        )


class DragMath(unittest.TestCase):
    ORIGIN = Vector((0.0, 0.0, 1.5))

    def px(self, rot, dist=3.0):
        return navigation.drag_pointer_pixels(self.ORIGIN, AIM_ROT, self.ORIGIN, rot, dist)

    def test_no_motion_is_center(self):
        self.assertEqual(self.px(AIM_ROT), (512, 512))

    def test_rotation_matches_blender_camera_projection(self):
        """Blender's own lens-to-FOV conversion with the C++ pick lens must give our half-angle."""
        cam_data = bpy.data.cameras.new("pick")
        cam_data.sensor_fit = "AUTO"
        cam_data.sensor_width = 36.0
        cam_data.lens = 18.0 / tan(navigation.POINTER_PICK_HALF_ANGLE)  # same formula as C++
        self.assertAlmostEqual(cam_data.angle / 2.0, navigation.POINTER_PICK_HALF_ANGLE, places=4)

        # Reference pinhole projection of the point the new aim ray hits on the press-time plane.
        forward0 = AIM_ROT @ Vector((0, 0, -1))
        right0 = AIM_ROT @ Vector((1, 0, 0))
        up0 = AIM_ROT @ Vector((0, 1, 0))
        for yaw_deg, pitch_deg in ((1.0, 0.0), (-2.0, 0.0), (0.0, 1.5), (3.0, -2.5), (4.0, 4.0)):
            delta = Euler((radians(pitch_deg), 0.0, radians(yaw_deg)), "XYZ").to_quaternion()
            rot = delta @ AIM_ROT
            direction = rot @ Vector((0, 0, -1))
            depth = direction.dot(forward0)
            expected = (
                512 + 512 * (direction.dot(right0) / depth) / tan(navigation.POINTER_PICK_HALF_ANGLE),
                512 + 512 * (direction.dot(up0) / depth) / tan(navigation.POINTER_PICK_HALF_ANGLE),
            )
            got = self.px(rot)
            self.assertLess(abs(got[0] - expected[0]), 3.0, (yaw_deg, pitch_deg, got, expected))
            self.assertLess(abs(got[1] - expected[1]), 3.0, (yaw_deg, pitch_deg, got, expected))

    def test_directions(self):
        yaw_left = Euler((0, 0, radians(2))).to_quaternion() @ AIM_ROT
        self.assertLess(self.px(yaw_left)[0], 512, "aiming left must move the pointer left")
        pitch_up = Euler((radians(2), 0, 0)).to_quaternion() @ AIM_ROT
        self.assertGreater(self.px(pitch_up)[1], 512, "aiming up must move the pointer up")

    def test_distance_scaling(self):
        delta = Euler((0, 0, radians(2))).to_quaternion() @ AIM_ROT
        self.assertLess(abs(self.px(delta, dist=0.5)[0] - self.px(delta, dist=20.0)[0]), 2)

    def test_drag_exceeds_threshold_quickly(self):
        delta = Euler((0, 0, radians(1))).to_quaternion() @ AIM_ROT
        self.assertGreater(abs(self.px(delta)[0] - 512), 20)


class FakeSession:
    """Stands in for bpy.types.XrSessionState; records every injected mouse event."""

    def __init__(self):
        self.events = []
        self.action_values = {}
        self.aim_loc = Vector((0.0, 0.0, 1.5))
        self.aim_rot = AIM_ROT.copy()
        self.viewer_pose_location = Vector((0, 0, 1.7))
        self.viewer_pose_rotation = Quaternion((1, 0, 0, 0))
        self.navigation_location = Vector((0, 0, 0))
        self.navigation_rotation = Quaternion((1, 0, 0, 0))
        self.navigation_scale = 1.0
        self.reject = False

    def action_state_get(self, _context, _map, action, _hand):
        return (self.action_values.get(action.replace("blendedr_", ""), 0.0),)

    def controller_aim_location_get(self, _context, _hand):
        return tuple(self.aim_loc)

    def controller_aim_rotation_get(self, _context, _hand):
        return tuple(self.aim_rot)

    def mouse_event_add(self, _context, event_type, value, **kwargs):
        if self.reject:
            raise RuntimeError("rejected")
        self.events.append((event_type, value, kwargs.get("x"), kwargs.get("y")))
        return True


def fake_bpy(session):
    class Prefs:
        disable_builtin_navigation = True
        sync_host_view = False
        capture_navigation_log = False
        orbit_sensitivity = pan_sensitivity = zoom_sensitivity = 1.0

    class Settings:
        controller_draw_style = "LIGHT_RAY"
        clip_end = 100.0
        base_pose_type = "CUSTOM"
        base_pose_angle = 0.0

    class Scene:
        class cursor:
            location = Vector((0, 0, 0))

        def ray_cast(self, *_args, **_kwargs):
            return (False, None, None, 0, None, None)

    context = types.SimpleNamespace(
        window_manager=types.SimpleNamespace(
            xr_session_state=session, xr_session_settings=Settings()
        ),
        preferences=types.SimpleNamespace(
            addons={runtime.__package__: types.SimpleNamespace(preferences=Prefs())}
        ),
        scene=Scene(),
        active_object=None,
        selected_objects=[],
        mode="OBJECT",
        evaluated_depsgraph_get=lambda: None,
    )
    return types.SimpleNamespace(
        context=context,
        types=types.SimpleNamespace(
            XrSessionState=types.SimpleNamespace(is_running=lambda _c: True)
        ),
        app=bpy.app,
    )


class PointerTimerReplay(unittest.TestCase):
    """Drives the real _pointer_timer with scripted controller input."""

    def setUp(self):
        self.session = FakeSession()
        self.saved = (navigation.bpy, runtime.bpy)
        navigation.bpy = runtime.bpy = fake_bpy(self.session)
        runtime.reset_input_state()
        runtime.debug_state["action_set_active"] = True

    def tearDown(self):
        navigation.bpy, runtime.bpy = self.saved
        runtime.reset_input_state()

    def tick(self, n=1, **action_values):
        self.session.action_values.update(action_values)
        for _ in range(n):
            navigation._pointer_timer()

    def aim_yaw(self, degrees):
        self.session.aim_rot = Euler((0, 0, radians(degrees))).to_quaternion() @ AIM_ROT

    def kind(self, name):
        return [e for e in self.session.events if e[0] == name]

    def test_click_without_motion(self):
        self.tick(2)
        self.tick(2, right_trigger=1.0)
        self.tick(2, right_trigger=0.0)
        kinds = [(e[0], e[1]) for e in self.session.events if e[0] != "MOUSEMOVE"]
        self.assertEqual(kinds, [("LEFTMOUSE", "PRESS"), ("LEFTMOUSE", "RELEASE")])
        for event in self.kind("LEFTMOUSE"):
            self.assertEqual(event[2:], (512, 512), "a click must stay on the pick-grid center")

    def test_drag_moves_follow_press(self):
        self.tick(1, right_trigger=1.0)
        for yaw in (0.5, 1.0, 1.5, 2.0):
            self.aim_yaw(yaw)
            self.tick(1)
        press_index = next(
            i for i, e in enumerate(self.session.events) if e[:2] == ("LEFTMOUSE", "PRESS")
        )
        xs = [e[2] for e in self.session.events[press_index:] if e[0] == "MOUSEMOVE"]
        self.assertGreaterEqual(len(xs), 4)
        self.assertTrue(all(b <= a for a, b in zip(xs, xs[1:])), f"non-monotonic drag: {xs}")
        self.assertLess(xs[-1], 512 - 20, f"drag never cleared the drag threshold: {xs}")

    def test_release_follows_last_drag_position(self):
        self.tick(1, right_trigger=1.0)
        self.aim_yaw(2.0)
        self.tick(2)
        self.tick(1, right_trigger=0.0)
        release = self.kind("LEFTMOUSE")[-1]
        self.assertEqual(release[1], "RELEASE")
        self.assertNotEqual(release[2], 512)

    def test_each_tick_emits_exactly_one_mousemove(self):
        self.tick(5)
        self.assertEqual(len(self.kind("MOUSEMOVE")), 5)

    def test_rejected_press_is_retried_not_lost(self):
        self.session.reject = True
        self.tick(3, right_trigger=1.0)
        self.assertFalse(runtime.injected_buttons)
        self.session.reject = False
        self.tick(1)
        self.assertIn("LEFTMOUSE", runtime.injected_buttons)

    def test_release_clears_button_state(self):
        self.tick(1, right_trigger=1.0)
        self.assertIn("LEFTMOUSE", runtime.injected_buttons)
        self.tick(1, right_trigger=0.0)
        self.assertNotIn("LEFTMOUSE", runtime.injected_buttons)

    def test_right_click(self):
        self.tick(2, left_trigger=1.0)
        self.tick(2, left_trigger=0.0)
        kinds = [(e[0], e[1]) for e in self.session.events if e[0] != "MOUSEMOVE"]
        self.assertEqual(kinds, [("RIGHTMOUSE", "PRESS"), ("RIGHTMOUSE", "RELEASE")])

    def test_baseline_cleared_after_release(self):
        self.tick(1, right_trigger=1.0)
        self.assertIsNotNone(runtime.drag_start_aim_loc)
        self.tick(1, right_trigger=0.0)
        self.assertIsNone(runtime.drag_start_aim_loc)


class AnalyzerFixtures(unittest.TestCase):
    """The failure-attribution tool must classify known-good and known-bad C++ traces correctly."""

    def run_analyzer(self, name):
        import subprocess

        fixtures = Path(__file__).resolve().parent / "fixtures"
        proc = subprocess.run(
            [
                sys.executable if Path(sys.executable).name.startswith("python") else "python",
                str(ROOT / "analyze_xr_interaction.py"),
                "--nav", str(fixtures / "missing.jsonl"),
                "--trace", str(fixtures / f"{name}.jsonl"),
            ],
            capture_output=True,
            text=True,
        )
        return proc.returncode, proc.stdout

    def test_healthy_drag_passes(self):
        code, _out = self.run_analyzer("healthy_drag")
        self.assertEqual(code, 0)

    def test_consumed_press_is_attributed(self):
        code, out = self.run_analyzer("press_consumed")
        self.assertEqual(code, 1)
        self.assertIn("press handling", out)

    def test_flat_projection_is_attributed(self):
        code, out = self.run_analyzer("no_projection")
        self.assertEqual(code, 1)
        self.assertIn("pointer projection", out)


class BuildIsCurrent(unittest.TestCase):
    """Guards against testing a stale binary: the exe must be newer than every edited C++ file."""

    def test_binary_newer_than_xr_sources(self):
        import subprocess

        exe = Path(bpy.app.binary_path)
        changed = subprocess.run(
            ["git", "-C", str(REPO), "diff", "--name-only", "HEAD", "--", "source"],
            capture_output=True, text=True,
        ).stdout.split()
        stale = [f for f in changed if (REPO / f).stat().st_mtime > exe.stat().st_mtime]
        self.assertFalse(stale, f"blender.exe is older than: {stale}. Run build/rebuild.cmd.")


if __name__ == "__main__":
    unittest.main(argv=["x"])
