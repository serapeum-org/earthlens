//VERSION=3
// Sentinel-2 SAVI (Soil-Adjusted Vegetation Index, L=0.5), FLOAT32.
function setup() {
  return { input: ["B04", "B08"], output: { bands: 1, sampleType: "FLOAT32" } };
}
function evaluatePixel(s) {
  return [1.5 * (s.B08 - s.B04) / (s.B08 + s.B04 + 0.5)];
}
