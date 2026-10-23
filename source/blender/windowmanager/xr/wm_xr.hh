/* SPDX-FileCopyrightText: 2023 Blender Authors
 *
 * SPDX-License-Identifier: GPL-2.0-or-later */

/** \file
 * \ingroup wm
 */

#pragma once

namespace blender {

struct wmWindow;
struct wmWindowManager;
struct wmXrData;

using wmXrSessionExitFn = void (*)(const wmXrData *xr_data);

/* `wm_xr.cc` */

bool wm_xr_init(bContext *C);
void wm_xr_exit(wmWindowManager *wm);
void wm_xr_session_toggle(wmWindowManager *wm, wmXrSessionExitFn session_exit_fn);
bool wm_xr_events_handle(wmWindowManager *wm);

/**
 * Prepare the XR offscreen area's window region for GPU picking with pointer button events:
 * point its view matrix along the right controller's aim pose and ensure that the region
 * viewport is available to the select engine. Defined in `wm_xr_session.cc`.
 */
void wm_xr_session_pointer_pick_setup(const bContext *xr_context, wmXrData *xr);

/* `wm_xr_operators.cc` */

void wm_xr_operatortypes_register();

/* `wm_xr_location_scouting.cc` */

/* NOTE: Keep in sync with the Python VR Scene Inspection add-on VRCapture class.
 *       See comment in #wm_xr_location_scouting_get_active_capture. */
struct XrLocationScoutingCapture {
  float3 position;
  float4 orientation_quat;

  float lens_focal;

  bool dof_enabled;
  float dof_distance;
  float dof_fstop;
};

bool wm_xr_location_scouting_is_captures_empty(Scene *scene);
std::optional<XrLocationScoutingCapture> wm_xr_location_scouting_get_active_capture(Scene *scene);

}  // namespace blender
