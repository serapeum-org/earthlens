//VERSION=3
// Sentinel-2 EVI (Enhanced Vegetation Index), single FLOAT32 band.
function setup() {
  return {
    input: ["B02", "B04", "B08"],
    output: { bands: 1, sampleType: "FLOAT32" },
  };
}
function evaluatePixel(s) {
  return [2.5 * (s.B08 - s.B04) / (s.B08 + 6.0 * s.B04 - 7.5 * s.B02 + 1.0)];
}
