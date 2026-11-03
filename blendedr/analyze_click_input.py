#!/usr/bin/env python3
"""Summarize XR trigger-to-pointer-click telemetry captured by the BlendedR add-on."""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_LOG = Path(__file__).resolve().parent / "build" / "blendedr_navigation.jsonl"
TRIGGERS = {"right_trigger": "LEFTMOUSE", "left_trigger": "RIGHTMOUSE"}
BUTTONS = {"LEFTMOUSE", "MIDDLEMOUSE", "RIGHTMOUSE"}


def read_entries(path):
    entries = []
    with path.open(encoding="utf-8") as log_file:
        for line_number, line in enumerate(log_file, 1):
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as error:
                print(f"Skipping invalid JSON on line {line_number}: {error}")
    return entries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_path", nargs="?", type=Path, default=DEFAULT_LOG)
    args = parser.parse_args()
    if not args.log_path.is_file():
        parser.error(f"Log file does not exist: {args.log_path}")

    entries = read_entries(args.log_path)
    actions = [entry for entry in entries if entry.get("event") == "action"]
    trigger_actions = [entry for entry in actions if entry.get("action") in TRIGGERS]
    native_events = [entry for entry in entries if entry.get("event") == "native_mouse_event"]
    click_events = [entry for entry in native_events if entry.get("type") in BUTTONS]

    print(f"Log: {args.log_path}")
    print(f"Entries: {len(entries)}")

    setup = [entry for entry in entries if entry.get("event") == "input_setup"]
    if setup:
        latest = setup[-1]
        print(f"Input setup: {'active' if latest.get('active') else 'failed'}")
        if latest.get("error"):
            print(f"  Error: {latest['error']}")

    trigger_counts = defaultdict(Counter)
    for entry in trigger_actions:
        trigger_counts[entry["action"]][entry.get("value", "?")] += 1
    print(f"Trigger actions: {len(trigger_actions)}")
    for action, values in sorted(trigger_counts.items()):
        print(f"  {action} -> {TRIGGERS[action]}: {dict(values)}")

    click_counts = Counter(
        (entry.get("type"), entry.get("value"), bool(entry.get("queued")))
        for entry in click_events
    )
    queued = [entry for entry in click_events if entry.get("queued")]
    rejected = [entry for entry in click_events if not entry.get("queued")]
    print(f"Injected button events: {len(click_events)} ({len(queued)} queued, {len(rejected)} rejected)")
    for (button, value, was_queued), count in sorted(click_counts.items()):
        print(f"  {button} {value}: {count} ({'queued' if was_queued else 'rejected'})")

    ray_events = [entry for entry in queued if "ray_hit" in entry]
    if ray_events:
        ray_hits = [entry for entry in ray_events if entry.get("ray_hit")]
        print(f"Laser ray predictions: {len(ray_hits)}/{len(ray_events)} queued events hit scene geometry")
        by_object = Counter(entry.get("ray_object") for entry in ray_hits)
        for object_name, count in by_object.most_common():
            print(f"  {object_name}: {count}")
        for entry in ray_events[-10:]:
            target = entry.get("ray_object") if entry.get("ray_hit") else "nothing"
            distance = entry.get("ray_distance")
            distance_text = f" at {distance:.3f}" if isinstance(distance, (int, float)) else ""
            print(f"  {entry.get('type')} {entry.get('value')} -> {target}{distance_text}")

    selection_events = [entry for entry in entries if entry.get("event") == "selection_change"]
    if selection_events:
        print(f"Observed selection changes: {len(selection_events)}")
        for entry in selection_events[-10:]:
            prev = entry.get("previous_active_object") or "None"
            curr = entry.get("active_object") or "None"
            selected = entry.get("selected_objects", [])
            print(f"  Active: {prev} -> {curr} | Selected: {selected}")

    transform_events = [entry for entry in entries if entry.get("event") == "object_transform"]
    if transform_events:
        print(f"Observed object transforms: {len(transform_events)}")
        for entry in transform_events[-5:]:
            print(f"  Transform '{entry.get('object')}': loc={entry.get('location')} delta={entry.get('delta')}")

    if rejected:
        print("Diagnosis: Blender rejected pointer events; inspect the first reported error:")
        print(f"  {rejected[0].get('error')}")
    elif trigger_actions and not click_events:
        print("Diagnosis: triggers arrived but no pointer button changed; inspect action-state polling.")
    elif click_events and not queued:
        print("Diagnosis: all injected clicks were rejected.")
    elif selection_events:
        print("Diagnosis: clicks succeeded and selection changed in the scene!")
    elif queued:
        print("Transport diagnosis: clicks reached Blender's XR event queue.")
        print("Check if laser ray predictions aligned with scene objects.")


if __name__ == "__main__":
    main()
