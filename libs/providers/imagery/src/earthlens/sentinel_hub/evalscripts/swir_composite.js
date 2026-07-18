//VERSION=3
// Sentinel-2 SWIR composite (RGB = B12, B08, B04) — burn scars, geology.
function setup() {
  return {
    input: ["B04", "B08", "B12"],
    output: { bands: 3, sampleType: "FLOAT32" },
  };
}
function evaluatePixel(s) {
  return [s.B12, s.B08, s.B04];
}
