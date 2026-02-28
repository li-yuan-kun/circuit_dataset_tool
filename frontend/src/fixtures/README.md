# Visual regression fixtures

- `min_visual_regression_scene.json`: Minimal manual-check scene for adjacent rectangular devices with edge pins (`p_right` -> `p_left`).
- Acceptance focus:
  1. Endpoint lead-in/lead-out segments are visible.
  2. Trunk avoids hugging device borders where possible.
  3. Net endpoints are visually obvious and not swallowed by node border strokes.
- `endpoint_cross_start_node_should_fail_scene.json`: Regression scene where a manual path trunk crosses back through the source device body; should be rejected and rerouted.

