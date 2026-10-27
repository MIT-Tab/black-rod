document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".speaks-score-lookup").forEach((form) => {
    const source = document.getElementById(form.dataset.valuesId);
    const input = form.querySelector("input");
    const output = form.querySelector("output");
    const rows = JSON.parse(source.textContent);
    const sampleSize = Number(form.dataset.sampleSize);

    const update = () => {
      const score = Number(input.value);
      if (input.value.trim() === "" || !Number.isFinite(score)) {
        output.textContent = "Enter a score to see its percentile.";
        return;
      }
      let cumulative = 0;
      rows.forEach((row) => {
        if (Number(row.score) <= score) {
          cumulative = row.cumulative_count;
        }
      });
      const percentile = ((cumulative * 100) / sampleSize).toFixed(2);
      output.textContent =
        `Percentile ${percentile}: ${cumulative.toLocaleString()} of ` +
        `${sampleSize.toLocaleString()} samples at or below ${score} speaks.`;
    };
    form.addEventListener("submit", (event) => event.preventDefault());
    input.addEventListener("input", update);
    update();
  });
});
