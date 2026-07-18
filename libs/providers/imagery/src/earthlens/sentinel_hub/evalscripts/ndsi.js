//VERSION=3
// Sentinel-2 NDSI (Normalized Difference Snow Index), FLOAT32.
function setup() {
  return { input: ["B03", "B11"], output: { bands: 1, sampleType: "FLOAT32" } };
}
function evaluatePixel(s) {
  return [(s.B03 - s.B11) / (s.B03 + s.B11)];
}
