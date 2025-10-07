/* global Chart */

const chartColors = {
  primary: "#045fb0",
  success: "#70A363",
  danger: "#dc3545",
  warning: "#668586",
  purple: "#6f42c1",
  teal: "#20c997",
};

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
        grid: {
          display: false,
        },
        offset: true,
      },
      y: {
        beginAtZero: true,
        grid: {
          color: "rgba(0, 0, 0, 0.05)",
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
    const chart = new Chart(document.getElementById(elementId), config);
    return chart;
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
          borderColor: chartColors.primary,
          backgroundColor: `${chartColors.primary}20`,
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
          borderColor: chartColors.warning,
          backgroundColor: `${chartColors.warning}20`,
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
          backgroundColor: chartColors.success,
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
          backgroundColor: chartColors.primary,
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
          borderColor: chartColors.teal,
          backgroundColor: `${chartColors.teal}20`,
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
          borderColor: chartColors.purple,
          backgroundColor: `${chartColors.purple}20`,
          fill: true,
          tension: 0.4,
        },
      ],
    },
    options: commonOptions,
  });

  tabPanes.forEach((pane, index) => {
    if (index !== 0) {
      pane.classList.remove("show", "active");
    }
  });
});
