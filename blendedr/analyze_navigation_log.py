#!/usr/bin/env python3
"""Summarize BlendedR navigation telemetry captured by the add-on."""

import argparse
import json
from collections import Counter
from math import sqrt
from pathlib import Path

DEFAULT_LOG = Path(__file__).resolve().parent / "build" / "blendedr_navigation.jsonl"


def vector_distance(before, after):
    if before is None or after is None:
        return None
    return sqrt(sum((current - previous) ** 2 for previous, current in zip(before, after)))


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
    parser.add_argument(
        "log_path",
        nargs="?",
        type=Path,
        default=DEFAULT_LOG,
        help=f"Telemetry JSONL path (default: {DEFAULT_LOG})",
    )
    arguments = parser.parse_args()
    path = arguments.log_path
    if not path.is_file():
        parser.error(f"Log file does not exist: {path}")

    entries = read_entries(path)
    event_counts = Counter(entry["event"] for entry in entries)
    print(f"Log: {path}")
    print(f"Entries: {len(entries)}")
    print("Events:")
    for event, count in sorted(event_counts.items()):
        print(f"  {event}: {count}")

    actions = [entry for entry in entries if entry["event"] == "action"]
    if actions:
        print("Actions:")
        for action, count in sorted(Counter(entry["action"] for entry in actions).items()):
            values = Counter(entry["value"] for entry in actions if entry["action"] == action)
            print(f"  {action}: {count} ({dict(values)})")

    ticks = [entry for entry in entries if entry["event"] == "navigation_tick"]
    accepted_ticks = [entry for entry in ticks if entry["delta_accepted"]]
    rejected_ticks = [entry for entry in ticks if not entry["delta_accepted"]]
    print(f"Navigation ticks: {len(accepted_ticks)} accepted, {len(rejected_ticks)} rejected")
    if accepted_ticks:
        yaw_deltas = [abs(entry["yaw_delta"]) for entry in accepted_ticks]
        pitch_deltas = [abs(entry["pitch_delta"]) for entry in accepted_ticks]
        print(f"Largest accepted aim delta: yaw={max(yaw_deltas):.5f}, pitch={max(pitch_deltas):.5f} rad")

    navigation_changes = []
    scale_changes = []
    for entry in accepted_ticks:
        navigation_change = vector_distance(
            entry["before_navigation_location"], entry["after_navigation_location"]
        )
        scale_change = entry["after_navigation_scale"] - entry["before_navigation_scale"]
        navigation_changes.append(navigation_change)
        scale_changes.append(scale_change)

    # XR action synchronization updates viewer_pose between timer callbacks, rather than while a
    # single navigation callback is executing. Compare the prior tick's final snapshot to this
    # tick's initial snapshot so a nonzero result represents observed XR viewer movement.
    viewer_changes_between_ticks = []
    previous_tick = None
    for entry in ticks:
        if previous_tick is not None:
            viewer_change = vector_distance(
                previous_tick["after_viewer_location"], entry["before_viewer_location"]
            )
            if viewer_change is not None:
                viewer_changes_between_ticks.append(viewer_change)
        previous_tick = entry

    if navigation_changes:
        changed_navigation = sum(change > 1e-7 for change in navigation_changes)
        changed_scale = sum(abs(change) > 1e-7 for change in scale_changes)
        print(
            "Authoritative state changes: "
            f"navigation location={changed_navigation}/{len(navigation_changes)}, "
            f"navigation scale={changed_scale}/{len(scale_changes)}"
        )
        print(
            "Largest per-tick write: "
            f"navigation location={max(navigation_changes):.6f}, "
            f"navigation scale={max(abs(change) for change in scale_changes):.6f}"
        )

    if viewer_changes_between_ticks:
        changed_viewer = sum(change > 1e-7 for change in viewer_changes_between_ticks)
        print(
            "XR viewer movement between navigation ticks: "
            f"{changed_viewer}/{len(viewer_changes_between_ticks)} changed, "
            f"largest={max(viewer_changes_between_ticks):.6f}"
        )

    begin_modes = Counter(entry["mode"] for entry in entries if entry["event"] == "navigation_begin")
    if begin_modes:
        print(f"Navigation gestures by initial mode: {dict(begin_modes)}")
    mode_changes = [entry for entry in entries if entry["event"] == "navigation_mode_change"]
    if mode_changes:
        changes_by_mode = Counter(entry["mode"] for entry in mode_changes)
        print(f"In-gesture navigation mode changes: {dict(changes_by_mode)}")

    native_events = [entry for entry in entries if entry["event"] == "native_mouse_event"]
    if native_events:
        queued = sum(entry["queued"] for entry in native_events)
        print(f"Native mouse transport: {queued}/{len(native_events)} queued")

    state_polls = [entry for entry in entries if entry["event"] == "state_poll"]
    if state_polls:
        print(f"Polled input-state corrections: {len(state_polls)}")
        for entry in state_polls[:10]:
            print(
                f"  modifiers {entry.get('previous_modifiers')} -> {entry.get('modifiers')}, "
                f"buttons {entry.get('previous_buttons')} -> {entry.get('buttons')}"
            )


if __name__ == "__main__":
    main()
