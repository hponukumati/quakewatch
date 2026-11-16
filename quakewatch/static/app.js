// Draws the charts described in figures.js. This is an external file (never inline) so the
// page's Content-Security-Policy can stay at script-src 'self'.
"use strict";

(function () {
  const figures = window.QUAKEWATCH_FIGURES || {};
  // scrollZoom off: scrolling the page over the map must scroll, not zoom.
  const config = { responsive: true, displaylogo: false, scrollZoom: false };
  for (const [name, figure] of Object.entries(figures)) {
    const element = document.getElementById("chart-" + name);
    if (element) {
      window.Plotly.newPlot(element, figure.data, figure.layout, config);
    }
  }
})();
