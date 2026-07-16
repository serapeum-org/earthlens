//VERSION=3
// Sentinel-2 BSI for the Statistical API (with the required dataMask band).
function setup() {
  return {
    input: ["B02", "B04", "B08", "B11", "dataMask"],
    output: [
      { id: "bsi", bands: 1, sampleType: "FLOAT32" },
      { id: "dataMask", bands: 1 },
    ],
  };
}
function evaluatePixel(s) {
  var num = (s.B11 + s.B04) - (s.B08 + s.B02);
  var den = (s.B11 + s.B04) + (s.B08 + s.B02);
  return { bsi: [num / den], dataMask: [s.dataMask] };
}
