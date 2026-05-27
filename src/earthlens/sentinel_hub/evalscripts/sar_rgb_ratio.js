//VERSION=3
// Sentinel-1 IW dual-pol composite (R=VV, G=VH, B=VV/VH), FLOAT32.
function setup() {
  return { input: ["VV", "VH"], output: { bands: 3, sampleType: "FLOAT32" } };
}
function evaluatePixel(s) {
  return [s.VV, s.VH, s.VV / (s.VH + 1e-9)];
}
