//VERSION=3
// Sentinel-2 NDMI (Normalized Difference Moisture Index), FLOAT32.
function setup() {
  return { input: ["B08", "B11"], output: { bands: 1, sampleType: "FLOAT32" } };
}
function evaluatePixel(s) {
  return [(s.B08 - s.B11) / (s.B08 + s.B11)];
}
