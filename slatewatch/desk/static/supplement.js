const basis = document.getElementById("supplement-basis");
const currentScan = document.getElementById("current-scan-field");
const scanInput = currentScan ? currentScan.querySelector("input") : null;

if (basis && currentScan && scanInput) {
  const sync = () => {
    const isCurrent = basis.value === "current";
    currentScan.hidden = !isCurrent;
    scanInput.required = isCurrent;
  };
  basis.addEventListener("change", sync);
  sync();
}
