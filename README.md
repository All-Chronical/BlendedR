BlendedR
========

BlendedR is a fork of [Blender](https://www.blender.org) 5.2 LTS that makes VR (OpenXR) input behave
like the desktop. The right controller's laser acts as the mouse, so you can select, drag gizmos, and
use Blender's own interface from inside the headset, without taking it off.

> BlendedR is an independent project. It is not affiliated with or endorsed by the Blender Foundation.
> "Blender" is a trademark of the Blender Foundation.

What it does
------------

- **Laser as mouse.** Right trigger is left click and left trigger is right click, aimed along the
  right controller's laser. Scene picking, selection and 3D gizmo dragging go through Blender's own
  event system.
- **Controller navigation.** Orbit, pan and zoom to the 3D Cursor from the controller buttons.
- **Viewport UI in VR.** The 3D View header floats in the headset as a world-fixed panel you point at
  and click. Open menus and popovers get their own panels next to it. While the laser is on the UI,
  the scene behind it gets no input.

Status: experimental. Developed and tested on Windows. Menus and popovers in VR are new and have had
less testing than the header. There is no keyboard text entry in VR yet.

Controls
--------

| Control | Action |
|---|---|
| Right trigger | Left click |
| Left trigger | Right click |
| Right A | Orbit (middle mouse) |
| Left A + right A | Pan |
| Left B + right A | Zoom toward the 3D Cursor |
| Right B | Alt |

Using it
--------

1. Build this repository (see below). The add-on needs the modified Blender: it does not work on
   official builds, which lack the XR additions it calls.
2. Install the `blendedr/addon` folder as an add-on and enable **BlendedR**.
3. Start a VR session from the 3D View as usual.

The **Capture Navigation Log** option in the add-on preferences writes a JSONL trace of controller and
pointer events, for diagnosing input problems.

Building
--------

Follow Blender's [build instructions](https://developer.blender.org/docs/handbook/building_blender/),
with OpenXR enabled (`WITH_XR_OPENXR`).

Repository layout
-----------------

- `source/`: Blender's source with BlendedR's changes. Commits that touch it are labelled `(src)`.
- `blendedr/addon/`: the Python add-on, where most of the logic lives.
- `blendedr/tests/`: add-on tests. `run_tests.cmd` runs them with the `blender.exe` built under
  `blendedr/build`.

BlendedR currently tracks Blender 5.2.2 LTS.

License
-------

Blender, and so BlendedR, is licensed under the GNU General Public License, Version 3 (see `COPYING`).
Individual files may have a different but compatible license. See
[blender.org/about/license](https://www.blender.org/about/license).
