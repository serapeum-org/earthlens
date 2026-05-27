//VERSION=3
// MODIS (MCD43A4) NDVI (NIR=B02, red=B01), FLOAT32.
function setup() {
  return { input: ["B01", "B02"], output: { bands: 1, sampleType: "FLOAT32" } };
}
function evaluatePixel(s) {
  return [(s.B02 - s.B01) / (s.B02 + s.B01)];
}
