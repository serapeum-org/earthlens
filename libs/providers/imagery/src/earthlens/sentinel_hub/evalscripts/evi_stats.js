//VERSION=3
// Sentinel-2 EVI for the Statistical API (with the required dataMask band).
function setup() {
  return {
    input: ["B02", "B04", "B08", "dataMask"],
    output: [
      { id: "evi", bands: 1, sampleType: "FLOAT32" },
      { id: "dataMask", bands: 1 },
    ],
  };
}
function evaluatePixel(s) {
  return {
    evi: [2.5 * (s.B08 - s.B04) / (s.B08 + 6.0 * s.B04 - 7.5 * s.B02 + 1.0)],
    dataMask: [s.dataMask],
  };
}
