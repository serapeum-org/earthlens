//VERSION=3
// Sentinel-2 agriculture composite (RGB = B11, B08, B02).
function setup() {
  return {
    input: ["B02", "B08", "B11"],
    output: { bands: 3, sampleType: "FLOAT32" },
  };
}
function evaluatePixel(s) {
  return [s.B11, s.B08, s.B02];
}
