//VERSION=3
// Sentinel-2 NBR (Normalized Burn Ratio), FLOAT32.
function setup() {
  return { input: ["B08", "B12"], output: { bands: 1, sampleType: "FLOAT32" } };
}
function evaluatePixel(s) {
  return [(s.B08 - s.B12) / (s.B08 + s.B12)];
}
