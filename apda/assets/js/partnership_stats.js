const initPartnershipStats = () => {
  const container = document.querySelector("[data-partnership-stats-url]");
  if (!container) {
    return;
  }

  async function loadStats() {
    try {
      const response = await fetch(container.dataset.partnershipStatsUrl, {
        cache: "no-store",
      });
      if (response.status === 202) {
        window.setTimeout(loadStats, 2000);
        return;
      }
      if (!response.ok) {
        throw new Error("Partnership stats request failed");
      }
      container.innerHTML = await response.text();
      container.setAttribute("aria-busy", "false");
    } catch {
      container.hidden = true;
      document.getElementById("partnership-stats-error").hidden = false;
    }
  }

  loadStats();
};

export default initPartnershipStats;
