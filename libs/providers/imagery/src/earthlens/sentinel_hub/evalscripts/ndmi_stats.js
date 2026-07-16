//VERSION=3
// Sentinel-2 NDMI for the Statistical API (with the required dataMask band).
function setup() {
  return {
    input: ["B08", "B11", "dataMask"],
    output: [
      { id: "ndmi", bands: 1, sampleType: "FLOAT32" },
      { id: "dataMask", bands: 1 },
    ],
  };
}
function evaluatePixel(s) {
  return { ndmi: [(s.B08 - s.B11) / (s.B08 + s.B11)], dataMask: [s.dataMask] };
}
