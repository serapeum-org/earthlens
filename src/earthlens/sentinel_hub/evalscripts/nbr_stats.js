//VERSION=3
// Sentinel-2 NBR for the Statistical API (with the required dataMask band).
function setup() {
  return {
    input: ["B08", "B12", "dataMask"],
    output: [
      { id: "nbr", bands: 1, sampleType: "FLOAT32" },
      { id: "dataMask", bands: 1 },
    ],
  };
}
function evaluatePixel(s) {
  return { nbr: [(s.B08 - s.B12) / (s.B08 + s.B12)], dataMask: [s.dataMask] };
}
