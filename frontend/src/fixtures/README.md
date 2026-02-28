# Visual regression fixtures

- `min_visual_regression_scene.json`: Minimal manual-check scene for adjacent rectangular devices with edge pins (`p_right` -> `p_left`).
- `outer_ring_detour_scene.json`: Extreme scene with a large middle obstacle between two endpoints, used to verify orthogonal routing can detour via outer top/bottom ring when center corridor is blocked.
- Acceptance focus:
  1. Endpoint lead-in/lead-out segments are visible.
  2. Trunk avoids hugging device borders where possible.
  3. Net endpoints are visually obvious and not swallowed by node border strokes.
