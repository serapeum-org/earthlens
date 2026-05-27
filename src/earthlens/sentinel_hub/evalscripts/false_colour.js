//VERSION=3
// Sentinel-2 false-colour (vegetation) composite (RGB = B08, B04, B03).
function setup() {
  return {
    input: ["B03", "B04", "B08"],
    output: { bands: 3, sampleType: "FLOAT32" },
  };
}
function evaluatePixel(s) {
  return [s.B08, s.B04, s.B03];
}
