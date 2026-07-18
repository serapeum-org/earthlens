//VERSION=3
// Sentinel-2 NDVI (Normalized Difference Vegetation Index), single FLOAT32 band.
function setup() {
  return { input: ["B04", "B08"], output: { bands: 1, sampleType: "FLOAT32" } };
}
function evaluatePixel(s) {
  return [(s.B08 - s.B04) / (s.B08 + s.B04)];
}
