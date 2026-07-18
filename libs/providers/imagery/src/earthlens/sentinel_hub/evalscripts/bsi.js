//VERSION=3
// Sentinel-2 BSI (Bare Soil Index), single FLOAT32 band.
function setup() {
  return {
    input: ["B02", "B04", "B08", "B11"],
    output: { bands: 1, sampleType: "FLOAT32" },
  };
}
function evaluatePixel(s) {
  var num = (s.B11 + s.B04) - (s.B08 + s.B02);
  var den = (s.B11 + s.B04) + (s.B08 + s.B02);
  return [num / den];
}
