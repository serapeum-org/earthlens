//VERSION=3
// Sentinel-3 SLSTR thermal brightness temperature (S8, ~11 um) in Kelvin.
function setup() {
  return { input: ["S8"], output: { bands: 1, sampleType: "FLOAT32" } };
}
function evaluatePixel(s) {
  return [s.S8];
}
