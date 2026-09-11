#!/usr/bin/env python3
"""Render the Healthbox card's drawing for a set of topologies, and check it.

Usage:
    python3 scripts/preview_card.py [outdir]

Why this exists: the card draws itself in the browser, and the test suite
has no browser. Every layout bug this card has had - a fixed viewport that
clipped a chain, branches fanned out instead of chained, a pictogram
landing on its own badge, blanking caps cut off the edges - was invisible
in the JavaScript and obvious the moment the drawing was rasterised. So
this runs the real card module (no reimplementation) against stub data and
looks at what comes out.

Two checks are automatic:

- every topology must keep the unit centred;
- no ink may touch the edge of the image, which is what clipping looks
  like once the drawing overflows its viewport.

Requires `node` on PATH. Rasterising and the clipping check additionally
need `cairosvg` and `pillow`; without them the SVGs are still written and
the centring check still runs.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from custom_components.healthbox3.card import build_card_module  # noqa: E402
from custom_components.healthbox3.scene_assets import SCENE_GEOMETRY  # noqa: E402

CENTRE = (
    SCENE_GEOMETRY["base_x"] + SCENE_GEOMETRY["base_size"] / 2,
    SCENE_GEOMETRY["base_y"] + SCENE_GEOMETRY["base_size"] / 2,
)

# One entry per shape worth looking at: the common case, a chain on each
# of the four edges, every port chained at once, and a unit with a single
# room - the two extremes of how lopsided an installation can be.
HARNESS = """
import fs from "node:fs";
globalThis.HTMLElement = class {
  querySelector() { return { style: {}, innerHTML: "" }; }
  querySelectorAll() { return []; }
  dispatchEvent() {}
};
const defined = new Map();
globalThis.customElements = { get: (n) => defined.get(n), define: (n, c) => defined.set(n, c) };
globalThis.window = globalThis;
await import("%(module)s");
const Card = defined.get("healthbox-card");

let uid = 0;
const room = (port, name, icon, error = false) =>
  ({ id: ++uid, port, name, icon, error, entities: { airflow: "s.a", airflow_rate: "s.b" } });
const chain = (port, n, name, icon, errIdx = -1) =>
  Array.from({ length: n }, (_, i) => room(port, `${name} ${i + 1}`, icon, i === errIdx));

const CASES = {
  "single outlets": [room(1, "Bathroom", "custom:renson-bath"),
                     room(2, "Toilet", "custom:renson-toilet"),
                     room(6, "Kitchen", "custom:renson-kitchen")],
  "chain right": [...chain(2, 2, "Bedroom", "custom:renson-bed"),
                  room(6, "Kitchen", "custom:renson-kitchen")],
  "chain left": [...chain(6, 3, "Office", "custom:renson-office", 1),
                 room(1, "Bathroom", "custom:renson-bath")],
  "chain top": [...chain(7, 2, "Attic", "custom:renson-storage"),
                room(2, "Toilet", "custom:renson-toilet")],
  "chain bottom": [...chain(3, 2, "Basement", "custom:renson-basement", 0),
                   room(6, "Kitchen", "custom:renson-kitchen")],
  "every port chained": [...chain(1, 3, "A", "custom:renson-bath"),
                         ...chain(2, 2, "B", "custom:renson-toilet"),
                         ...chain(3, 2, "C", "custom:renson-basement"),
                         ...chain(4, 3, "D", "custom:renson-washing", 2),
                         ...chain(5, 2, "E", "custom:renson-dresser"),
                         ...chain(6, 3, "F", "custom:renson-kitchen"),
                         ...chain(7, 2, "G", "custom:renson-storage")],
  "one room only": [room(4, "Laundry", "custom:renson-washing")],
};

const states = { "s.a": { state: "66" }, "s.b": { state: "21" } };
const out = {};
for (const [label, rooms] of Object.entries(CASES)) {
  const layout = { units: [{ serial: "T", name: "Healthbox 3.0",
                             unattributed_errors: 0, rooms }] };
  const card = new Card();
  card.setConfig({});
  card._layout = layout;
  card.hass = { states, callApi: async () => layout };
  const html = card.innerHTML;
  const svg = html.slice(html.indexOf("<svg"), html.indexOf("</svg>") + 6)
    .replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ');
  const key = label.replace(/[^a-z0-9]+/gi, "_");
  fs.writeFileSync("%(outdir)s/" + key + ".svg", svg);
  out[label] = { key, viewBox: svg.match(/viewBox="([^"]+)"/)[1].split(" ").map(Number) };
}
fs.writeFileSync("%(outdir)s/cases.json", JSON.stringify(out));
"""


def _rasterise(svg_path: pathlib.Path, view_box: list[float]) -> object | None:
    """Render one drawing, or None if the optional deps are missing."""
    try:
        import io

        import cairosvg
        from PIL import Image
    except ImportError:
        return None

    svg = svg_path.read_text(encoding="utf-8")
    width = 700
    height = int(width * view_box[3] / view_box[2])
    svg = re.sub(
        r'style="width:100%;height:auto;display:block"',
        f'width="{width}" height="{height}"',
        svg,
        count=1,
    )
    # The card paints in theme variables; pick the light theme's values so
    # the result looks like what a browser would show.
    svg = (
        svg.replace("var(--card-background-color,#fff)", "#ffffff")
        .replace("var(--error-color,#db4437)", "#db4437")
        .replace("currentColor", "#494948")
    )
    png = cairosvg.svg2png(bytestring=svg.encode(), background_color="white")
    image = Image.open(io.BytesIO(png))
    image.save(svg_path.with_suffix(".png"))
    return image


def _edges_with_ink(image: object) -> list[str]:
    """Return which borders have ink on them - i.e. where it was clipped."""
    grey = image.convert("L")
    pixels = grey.load()
    width, height = grey.size
    sides = set()
    for x in range(width):
        if pixels[x, 0] < 250:
            sides.add("top")
        if pixels[x, height - 1] < 250:
            sides.add("bottom")
    for y in range(height):
        if pixels[0, y] < 250:
            sides.add("left")
        if pixels[width - 1, y] < 250:
            sides.add("right")
    return sorted(sides)


def main() -> None:
    """Render every topology and report."""
    outdir = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "card-preview")
    outdir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        module = pathlib.Path(tmp) / "healthbox-card.js"
        module.write_text(build_card_module(), encoding="utf-8")
        harness = pathlib.Path(tmp) / "harness.mjs"
        harness.write_text(
            HARNESS % {"module": module, "outdir": outdir.resolve()}, encoding="utf-8"
        )
        try:
            subprocess.run([
                "node", str(harness)], check=True, cwd=tmp)
        except FileNotFoundError:
            sys.exit("node is not on PATH; it is needed to run the card itself")

    cases = json.loads((outdir / "cases.json").read_text(encoding="utf-8"))
    failures = 0
    print(f"{'topology':<22} {'viewBox':<24} {'centred':<9} clipping")
    for label, info in cases.items():
        box = info["viewBox"]
        centred = (
            box[0] + box[2] / 2 == CENTRE[0] and box[1] + box[3] / 2 == CENTRE[1]
        )
        image = _rasterise(outdir / f"{info['key']}.svg", box)
        if image is None:
            verdict = "not checked (no cairosvg/pillow)"
        else:
            edges = _edges_with_ink(image)
            verdict = "none" if not edges else f"CLIPPED on {', '.join(edges)}"
            failures += bool(edges)
        failures += not centred
        print(
            f"{label:<22} {' '.join(map(str, box)):<24} "
            f"{'yes' if centred else 'NO':<9} {verdict}"
        )

    print(f"\nWritten to {outdir}")
    if failures:
        sys.exit(f"{failures} problem(s)")


if __name__ == "__main__":
    main()
