//VERSION=3
// Copernicus DEM elevation in metres, single FLOAT32 band.
function setup() {
  return { input: ["DEM"], output: { bands: 1, sampleType: "FLOAT32" } };
}
function evaluatePixel(s) {
  return [s.DEM];
}
