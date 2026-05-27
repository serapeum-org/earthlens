//VERSION=3
// Sentinel-2 NDWI for the Statistical API (with the required dataMask band).
function setup() {
  return {
    input: ["B03", "B08", "dataMask"],
    output: [
      { id: "ndwi", bands: 1, sampleType: "FLOAT32" },
      { id: "dataMask", bands: 1 },
    ],
  };
}
function evaluatePixel(s) {
  return {
    ndwi: [(s.B03 - s.B08) / (s.B03 + s.B08)],
    dataMask: [s.dataMask],
  };
}
