#!/usr/bin/env python3
"""Attribute an XR pointer interaction (click / drag / transform) to the pipeline stage that failed.

Inputs are the two logs written while "Capture navigation log" is enabled in the add-on:
  build/blendedr_navigation.jsonl   add-on side: injected events, drag moves, scene changes
  build/blendedr_xr_trace.jsonl     C++ side: what Blender's XR event dispatch did with each event

For every LEFTMOUSE/RIGHTMOUSE press the report walks the stages in order and stops at the first
one that did not happen, so a broken drag is not re-diagnosed from guesses. Exit code is 1 when any
gesture failed, so this can gate an automated check.
"""

import argparse
import json
import sys
from pathlib import Path

BUILD = Path(__file__).resolve().parent / "build"
LEFT, MIDDLE, RIGHT, MOUSEMOVE = 1, 2, 3, 4
PRESS, RELEASE = 1, 2
BUTTON_NAMES = {LEFT: "LEFTMOUSE", MIDDLE: "MIDDLEMOUSE", RIGHT: "RIGHTMOUSE"}
ACTION_BREAK, ACTION_HANDLED, ACTION_MODAL = 1, 2, 4
DRAG_PIXELS = 4  # Blender's default drag threshold is 3 px (scaled by UI scale).


def read_jsonl(path):
    entries = []
    if not path.is_file():
        return entries
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as error:
                print(f"  skipped invalid JSON on {path.name}:{number}: {error}")
    return entries


def describe_action(action):
    flags = [
        name
        for bit, name in ((ACTION_BREAK, "BREAK"), (ACTION_HANDLED, "HANDLED"), (ACTION_MODAL, "MODAL"))
        if action & bit
    ]
    return "|".join(flags) or "CONTINUE"


def group_cpp_events(cpp):
    """Fold the staged C++ records into one dict per dispatched event."""
    events, current = [], None
    for entry in cpp:
        stage = entry.get("stage")
        if stage == "event_in":
            current = {"in": entry, "drags": [], "modal_action": None, "out": None}
            events.append(current)
        elif current is None:
            continue
        elif stage == "modal_done":
            current["modal_action"] = entry.get("action")
        elif stage == "drag_generated":
            current["drags"].append(entry)
        elif stage == "event_out":
            current["out"] = entry
    return events


def gestures(events):
    """Split dispatched events into press..release gestures per button."""
    result, open_gestures = [], {}
    for event in events:
        kind, value = event["in"]["type"], event["in"]["val"]
        if kind in BUTTON_NAMES and value == PRESS:
            gesture = {"button": kind, "events": [event]}
            open_gestures[kind] = gesture
            result.append(gesture)
        elif kind in BUTTON_NAMES and value == RELEASE and kind in open_gestures:
            open_gestures.pop(kind)["events"].append(event)
        else:
            for gesture in open_gestures.values():
                gesture["events"].append(event)
    return result


def analyze_gesture(gesture, index, python_entries):
    events = gesture["events"]
    press = events[0]
    name = BUTTON_NAMES[gesture["button"]]
    moves = [e for e in events if e["in"]["type"] == MOUSEMOVE]
    released = events[-1]["in"]["val"] == RELEASE and events[-1]["in"]["type"] == gesture["button"]
    press_xy = press["in"]["xy"]
    max_pixels = max(
        (max(abs(e["in"]["xy"][0] - press_xy[0]), abs(e["in"]["xy"][1] - press_xy[1])) for e in moves),
        default=0,
    )
    drag_events = [d for e in events for d in e["drags"]]
    out = press["out"] or {}
    modal_seen = sorted({op for e in events for op in e["in"]["xr_modal_ops"].split(",") if op})
    gz_modal_seen = sorted({e["in"]["gz_modal"] for e in events if e["in"]["gz_modal"]})
    gz_press = press["in"]["gz_highlight"] or out.get("gz_highlight") or ""

    print(f"\n[{index}] {name} press at {press_xy}  (seq {press['in']['seq']})")
    print(f"    press: gizmo_highlight={gz_press or '-'} handled={describe_action(out.get('action', 0))} "
          f"check_drag_after={out.get('check_drag')} check_click_after={out.get('check_click')}")
    print(f"    pointer moves while held: {len(moves)}, max displacement {max_pixels}px, "
          f"region handlers ran on {sum(1 for e in moves if (e['out'] or {}).get('region_handlers'))} moves")
    print(f"    KM_PRESS_DRAG generated: {len(drag_events)}; XR modal operators seen: {modal_seen or '-'}; "
          f"modal gizmo: {gz_modal_seen or '-'}; released: {released}")

    failure = None
    if out.get("action", 0) & ACTION_HANDLED and not (modal_seen or gz_modal_seen):
        handled_note = " (a press handler consumed it, so drag detection is cancelled)"
    else:
        handled_note = ""
    if not moves:
        failure = ("python->queue: no MOUSEMOVE reached the C++ dispatcher while the button was held. "
                   "Check 'drag_move' entries in the add-on log (injection rejected or loop stalled).")
    elif max_pixels < DRAG_PIXELS:
        failure = (f"pointer projection: moves arrived but never left the press point by >= {DRAG_PIXELS}px "
                   f"(max {max_pixels}). Controller motion is not being projected into the pick grid.")
    elif not drag_events and not modal_seen and not gz_modal_seen:
        if out.get("check_drag") in (0, False):
            failure = ("press handling: the press cleared drag detection (check_drag=0), so KM_PRESS_DRAG can "
                       f"never fire{handled_note}. Look at which handler consumed the press.")
        else:
            failure = ("drag generation: check_drag was armed and the pointer moved far enough, but "
                       "wm_handlers_do never produced KM_PRESS_DRAG. Moves were not routed to region handlers "
                       "or drag detection was reset between events.")
    elif drag_events and not modal_seen and not gz_modal_seen:
        failure = ("drag handling: KM_PRESS_DRAG was generated but no XR modal operator or gizmo modal "
                   "started. The keymap/gizmo handler did not act on the drag (check gizmo highlight and "
                   "active tool).")
    elif not released:
        failure = "release: no matching RELEASE reached the dispatcher (modal operator may be stuck)."

    if failure:
        print(f"    FAILED AT -> {failure}")
        return False
    print("    all dispatch stages reached")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nav", type=Path, default=BUILD / "blendedr_navigation.jsonl")
    parser.add_argument("--trace", type=Path, default=BUILD / "blendedr_xr_trace.jsonl")
    args = parser.parse_args()

    python_entries = read_jsonl(args.nav)
    cpp_entries = read_jsonl(args.trace)
    print(f"add-on log: {len(python_entries)} entries ({args.nav})")
    print(f"C++ trace : {len(cpp_entries)} entries ({args.trace})")

    queued_presses = [
        e for e in python_entries
        if e.get("event") == "native_mouse_event" and e.get("value") == "PRESS" and e.get("queued")
    ]
    rejected = [e for e in python_entries if e.get("event") == "native_mouse_event" and not e.get("queued")]
    print(f"add-on injected {len(queued_presses)} button presses ({len(rejected)} rejected)")
    if rejected:
        print(f"  first rejection: {rejected[0].get('error')}")

    if not cpp_entries:
        print("\nFAILED AT -> C++ trace is empty. Either capture was not enabled before the session, or the "
              "running Blender build predates the trace (rebuild and relaunch via run_manual.cmd).")
        return 1

    events = group_cpp_events(cpp_entries)
    cpp_presses = [e for e in events if e["in"]["type"] in BUTTON_NAMES and e["in"]["val"] == PRESS]
    print(f"C++ dispatched {len(events)} XR pointer events, {len(cpp_presses)} button presses")
    if queued_presses and not cpp_presses:
        print("\nFAILED AT -> queue->dispatch: the add-on queued presses but the C++ dispatcher never saw one.")
        return 1

    ok = True
    for index, gesture in enumerate(gestures(events), 1):
        ok = analyze_gesture(gesture, index, python_entries) and ok

    transforms = [e for e in python_entries if e.get("event") == "object_transform"]
    selections = [e for e in python_entries if e.get("event") == "selection_change"]
    print(f"\nscene effects: {len(selections)} selection changes, {len(transforms)} object transforms")
    for entry in transforms[-5:]:
        print(f"  {entry.get('object')} moved by {entry.get('delta')}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
