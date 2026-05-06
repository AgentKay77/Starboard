// Chart.js initializers. Each chart-bearing template stamps a <canvas> with
// data-chart="rating-history" plus data-* JSON for the points; this script
// wires Chart.js against those.

(function () {
  if (typeof Chart === "undefined") return;

  Chart.defaults.color = "#cbd5e1";
  Chart.defaults.borderColor = "rgba(148, 163, 184, 0.15)";
  Chart.defaults.font.family = "'JetBrains Mono', ui-monospace, monospace";

  function ratingHistoryChart(canvas) {
    const labels = JSON.parse(canvas.dataset.labels || "[]");
    const data = JSON.parse(canvas.dataset.values || "[]");
    new Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "APR",
            data,
            borderColor: "#fbbf24",
            backgroundColor: "rgba(251, 191, 36, 0.1)",
            fill: true,
            tension: 0.3,
            pointRadius: 3,
            pointHoverRadius: 6,
            borderWidth: 2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { display: false } },
          y: {
            ticks: { precision: 0 },
            grid: { color: "rgba(148, 163, 184, 0.08)" },
          },
        },
      },
    });
  }

  document.querySelectorAll('canvas[data-chart="rating-history"]').forEach(ratingHistoryChart);
})();
