//VERSION=3
// Landsat 8/9 OLI NDVI for the Statistical API (with the required dataMask band).
function setup() {
  return {
    input: ["B04", "B05", "dataMask"],
    output: [
      { id: "ndvi", bands: 1, sampleType: "FLOAT32" },
      { id: "dataMask", bands: 1 },
    ],
  };
}
function evaluatePixel(s) {
  return { ndvi: [(s.B05 - s.B04) / (s.B05 + s.B04)], dataMask: [s.dataMask] };
}
