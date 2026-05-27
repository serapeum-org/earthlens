//VERSION=3
// Sentinel-3 OLCI true-colour composite (RGB = B08 ~665nm, B06 ~560nm, B04 ~490nm).
function setup() {
  return {
    input: ["B04", "B06", "B08"],
    output: { bands: 3, sampleType: "FLOAT32" },
  };
}
function evaluatePixel(s) {
  return [s.B08, s.B06, s.B04];
}
