//VERSION=3
// Sentinel-2 NDVI for the Statistical API — emits the required dataMask band so
// the server excludes invalid pixels from the zonal statistics.
function setup() {
  return {
    input: ["B04", "B08", "dataMask"],
    output: [
      { id: "ndvi", bands: 1, sampleType: "FLOAT32" },
      { id: "dataMask", bands: 1 },
    ],
  };
}
function evaluatePixel(s) {
  return {
    ndvi: [(s.B08 - s.B04) / (s.B08 + s.B04)],
    dataMask: [s.dataMask],
  };
}
