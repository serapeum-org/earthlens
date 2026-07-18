//VERSION=3
// Landsat 8/9 OLI NDVI (NIR=B05, red=B04), FLOAT32.
function setup() {
  return { input: ["B04", "B05"], output: { bands: 1, sampleType: "FLOAT32" } };
}
function evaluatePixel(s) {
  return [(s.B05 - s.B04) / (s.B05 + s.B04)];
}
