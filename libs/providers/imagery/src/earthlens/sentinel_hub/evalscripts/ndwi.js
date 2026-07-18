//VERSION=3
// Sentinel-2 NDWI (Normalized Difference Water Index, McFeeters), FLOAT32.
function setup() {
  return { input: ["B03", "B08"], output: { bands: 1, sampleType: "FLOAT32" } };
}
function evaluatePixel(s) {
  return [(s.B03 - s.B08) / (s.B03 + s.B08)];
}
