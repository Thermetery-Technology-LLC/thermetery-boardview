# Web-Based Canvas Core (Android WebView Renderer)

`viewer.js` (with `viewer.html` / `viewer.css`, under
`app/src/main/assets/`) implements the complete interactive boardview
experience that runs inside the Android WebView. It is deliberately
self-contained: zero network or external-library dependencies, works
from `file:///android_asset`.

## Architecture overview

1. **Ingest** — `window.bv.onBoard` / `onTraces` receive JSON payloads
   from the Kotlin host (produced by `board_export.py`). The data is
   normalised into typed arrays and spatial indices once.
2. **Static raster** — All non-interactive geometry (substrate,
   segments, vias, pins, component outlines, refdes labels) is drawn
   into an off-screen canvas at the settled view transform. During
   pan/pinch the raster is blitted with a delta transform.
3. **Live overlay** — Only the current selection (component outline +
   pin marker + label) is redrawn per frame on top of the blit.
4. **Settle timer** — 150 ms after the last gesture the static raster
   is recomputed so subsequent interaction stays crisp.

This separation keeps interaction at frame rate on large boards while
preserving pixel-perfect fidelity once the view settles.

## Key properties

- **Coordinate fidelity** — world y-up to screen y-down conversion,
  rotation about the viewport centre, and zoom/pan maths match the
  desktop viewer's visual results.
- **View mirror (BOTTOM flip)** — the desktop viewer
  (`CanvasCommon._apply_view_transform`) shows the BOTTOM layer mirrored
  about the board's x extent, "as if the board were flipped over", XOR a
  user *Mirror* toggle. `board_export.py` bakes no per-component mirror
  into coordinates, so the renderer does the same thing at view level:
  `view.k` stays positive and every world→screen **x** site uses the
  signed scale `kx = isMirrored() ? -k : k`, where
  `isMirrored() = (layerIdx === 1 /* BOTTOM */) !== userMirror`.
  Sizes, widths, radii and pixel thresholds keep using `k`; screen rects
  derived from a bbox are normalised (`compRect`) so culling and refdes
  placement work with a negative width; text is always drawn at computed
  screen coordinates, never through a mirrored canvas transform. The
  mirror happens in base (un-rotated) space, so the composite-time
  rotation is applied after it — the same order as the desktop.
  Cycling the layer button into/out of BOTTOM therefore flips the view
  automatically; the `Mirror` toolbar button (`#bv-btn-mirror`) flips
  `userMirror`, and its `.on` state/tooltip reflect the *effective*
  mirror. Both go through `reorient()`, which keeps the world point under
  the viewport centre fixed by reflecting `tx` about the centre and
  re-anchors a live pan/pinch gesture so a drag that continues after the
  toggle does not fling the board. `centerOn()` and `fitView()` place
  their target through `placeWorldAt()`, which inverse-rotates the final
  screen point into base space, so a centred pin lands on the intended
  screen point at any view rotation. The
  static raster records the mirror sign at capture (`raster.mirrored`);
  `frame()` re-captures immediately on a sign mismatch, even mid-gesture,
  because the delta blit assumes a positive scale ratio. `userMirror`
  resets on every new board.
- **Hit-testing** — x-sorted pin index + binary search for the
  x-window, then Euclidean distance. Component hit uses outline
  containment (bbox for tiny parts). Pin resolution is gated by
  on-screen pitch so dense BGA balls don't steal taps from the package
  body. At a TOP or BOTTOM view the tap prefers parts/pins on the visible
  layer (ghosted other-layer parts are only hit when nothing on the
  visible layer is), like the desktop's `_find_component_at`; ALL and
  inner-layer views take any layer.
- **Layer & net highlighting** — dim-alpha for everything else while a
  net is highlighted; bright passes for the selected net's
  pins/segments/vias. Palette matches the desktop viewer.
- **Memory discipline** — typed arrays (`Float64Array`, `Int32Array`)
  for geometry; label budget capped at 400 entries; raster size capped
  at 14 Mpixels to prevent runaway memory on high-DPR devices.
- **Contract compliance** — implements the surface defined in
  `docs/android_contract.md` (`window.bv` callbacks, `window.Android`
  bridge methods, status/toast/error channels).

## Performance model

| Phase | Cost |
|-------|------|
| Board ingest + index build | O(N log N) once (pin sort) |
| Rasterize | O(segments + pins + comps) at settle |
| Gesture frame | O(1) blit + O(1) selection |
| Tap hit-test | O(log N + K), K = pins in the x-window |

## Integration notes

- The host (Kotlin) injects board/traces JSON via `evaluateJavascript`
  calling `window.bv.onBoard` / `onTraces`.
- Key-manager and file-picker actions are delegated to the Android
  bridge (`window.Android.openFilePicker/openKeyManager`).
- Selection panel tabs: a canvas tap on a pin opens the **Net** tab
  (highlighting that pin's net); a body tap opens **Pins**. Tapping a row
  in the Net tab calls `selectComp(ci, pin, true, "net")`, which keeps the
  Net tab visible (highlight unchanged, tapped row marked `.sel`, canvas
  centred on that pin) while the Pins tab content is refreshed silently.
  `selectComp`'s `tab` argument is explicit (`"net"` | `"pins"`), never
  inferred. The Net list is capped at `MAX_NET_ROWS`; when the selected
  member falls past the cap its row is still rendered (between two
  "… N more" rows) so it can be scrolled into view, and `.bv-netrow` has
  a `scroll-margin-top` so the scrolled-to row clears the sticky net
  header.
- Desktop development is supported by opening the same file directly in
  a browser (drag-and-drop + sample.json auto-load). Scripted states:
  `?test=select`, `?test=layer` (BOTTOM, auto-mirrored) and
  `?test=mirror` (ALL + user Mirror). The dev harness also installs a
  read-only probe `window.bv.devView()` (view numbers, effective mirror,
  selection, a `w2s(wx, wy)` world→screen helper); it is only defined
  when `window.Android` is absent.
