//VERSION=3
// Sentinel-2 geology composite (RGB = B12, B11, B02).
function setup() {
  return {
    input: ["B02", "B11", "B12"],
    output: { bands: 3, sampleType: "FLOAT32" },
  };
}
function evaluatePixel(s) {
  return [s.B12, s.B11, s.B02];
}
