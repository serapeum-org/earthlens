//VERSION=3
// Sentinel-2 true-colour composite (RGB = B04, B03, B02), FLOAT32 reflectance.
function setup() {
  return {
    input: ["B02", "B03", "B04"],
    output: { bands: 3, sampleType: "FLOAT32" },
  };
}
function evaluatePixel(s) {
  return [s.B04, s.B03, s.B02];
}
