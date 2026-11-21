/* global Chart */

function getChartColors() {
  const styles = window.getComputedStyle(document.documentElement);
  const dark = document.documentElement.dataset.theme === "dark";
  return {
    primary: styles.getPropertyValue("--link").trim(),
    success: "#70A363",
    warning: dark ? "#9eb8bb" : "#668586",
    purple: dark ? "#b695f3" : "#6f42c1",
    teal: "#20c997",
    text: styles.getPropertyValue("--muted-ink").trim(),
    grid: dark
      ? styles.getPropertyValue("--line").trim()
      : "rgba(0, 0, 0, 0.05)",
  };
}

document.addEventListener("DOMContentLoaded", () => {
  // Get data from window object (passed from Django template)
  if (!window.statsData) {
    return;
  }

  const debatersData = window.statsData.debaters;
  const teamsData = window.statsData.teams;
  const totyData = window.statsData.toty;
  const tournamentsData = window.statsData.tournaments;
  const medianStatsData = window.statsData.medianStats;

  const tabPanes = document.querySelectorAll(".tab-pane");
  tabPanes.forEach((pane) => {
    pane.classList.add("show", "active");
  });

  let chartColors = getChartColors();
  const charts = [];

  const commonOptions = {
    responsive: true,
    maintainAspectRatio: true,
    animation: false,
    plugins: {
      legend: {
        display: false,
      },
    },
    scales: {
      x: {
        ticks: { color: () => chartColors.text },
        grid: {
          display: false,
        },
        offset: true,
      },
      y: {
        ticks: { color: () => chartColors.text },
        beginAtZero: true,
        grid: {
          color: () => chartColors.grid,
        },
      },
    },
    layout: {
      padding: {
        left: 10,
        right: 10,
      },
    },
  };

  const createChart = (elementId, config) => {
    charts.push(new Chart(document.getElementById(elementId), config));
  };

  const formatSeasonLabel = (d) =>
    `${d.season}-${(parseInt(d.season, 10) + 1).toString().slice(-2)}`;

  createChart("debatersChart", {
    type: "line",
    data: {
      labels: debatersData.map(formatSeasonLabel),
      datasets: [
        {
          label: "Debaters",
          data: debatersData.map((d) => d.count),
          borderColor: () => chartColors.primary,
          backgroundColor: () => `${chartColors.primary}20`,
          fill: true,
          tension: 0.4,
        },
      ],
    },
    options: commonOptions,
  });

  createChart("teamsChart", {
    type: "line",
    data: {
      labels: teamsData.map(formatSeasonLabel),
      datasets: [
        {
          label: "Unique Teams",
          data: teamsData.map((d) => d.count),
          borderColor: () => chartColors.warning,
          backgroundColor: () => `${chartColors.warning}20`,
          fill: true,
          tension: 0.4,
        },
      ],
    },
    options: commonOptions,
  });

  createChart("totyChart", {
    type: "bar",
    data: {
      labels: totyData.map(formatSeasonLabel),
      datasets: [
        {
          label: "Total TOTY Points",
          data: totyData.map((d) => d.total_points),
          backgroundColor: () => chartColors.success,
        },
      ],
    },
    options: commonOptions,
  });

  createChart("tournamentsChart", {
    type: "bar",
    data: {
      labels: tournamentsData.map(formatSeasonLabel),
      datasets: [
        {
          label: "Tournaments",
          data: tournamentsData.map((d) => d.tournament_count),
          backgroundColor: () => chartColors.primary,
        },
      ],
    },
    options: commonOptions,
  });

  createChart("medianSizeChart", {
    type: "line",
    data: {
      labels: medianStatsData.map(formatSeasonLabel),
      datasets: [
        {
          label: "Median Tournament Size (teams)",
          data: medianStatsData.map((d) => d.median_size),
          borderColor: () => chartColors.teal,
          backgroundColor: () => `${chartColors.teal}20`,
          fill: true,
          tension: 0.4,
        },
      ],
    },
    options: commonOptions,
  });

  createChart("medianNovicesChart", {
    type: "line",
    data: {
      labels: medianStatsData.map(formatSeasonLabel),
      datasets: [
        {
          label: "Median Novices per Tournament",
          data: medianStatsData.map((d) => d.median_novices),
          borderColor: () => chartColors.purple,
          backgroundColor: () => `${chartColors.purple}20`,
          fill: true,
          tension: 0.4,
        },
      ],
    },
    options: commonOptions,
  });

  document.addEventListener("standings:themechange", () => {
    chartColors = getChartColors();
    charts.forEach((chart) => chart.update("none"));
  });

  tabPanes.forEach((pane, index) => {
    if (index !== 0) {
      pane.classList.remove("show", "active");
    }
  });
});
