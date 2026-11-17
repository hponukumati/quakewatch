// Draws the charts described in figures.js. This is an external file (never inline) so the
// page's Content-Security-Policy can stay at script-src 'self'.
"use strict";

(function () {
  const figures = window.QUAKEWATCH_FIGURES || {};
  // scrollZoom off: scrolling the page over the map must scroll, not zoom.
  const config = { responsive: true, displaylogo: false, scrollZoom: false };
  // On phone-width screens the toolbar would sit on top of the chart titles, so hide it there.
  // Elsewhere keep Plotly's default, "hover" (true would show it all the time).
  config.displayModeBar = window.matchMedia("(max-width: 640px)").matches ? false : "hover";
  for (const [name, figure] of Object.entries(figures)) {
    const element = document.getElementById("chart-" + name);
    if (element) {
      window.Plotly.newPlot(element, figure.data, figure.layout, config);
    }
  }
})();
