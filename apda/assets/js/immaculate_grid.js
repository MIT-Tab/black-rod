function initImmaculateGrid() {
  const modal = document.getElementById("immaculate-guess");
  if (!modal) return;

  const grid = modal.closest("[data-immaculate-grid]");
  const form = modal.querySelector("form");
  const cell = form.elements.namedItem("cell");
  const debater = form.elements.namedItem("debater");
  const clue = modal.querySelector("[data-immaculate-clue]");
  const input = modal.querySelector("#immaculate-name");
  const status = modal.querySelector("[data-search-status]");
  const results = modal.querySelector("[data-search-results]");
  const submit = modal.querySelector("[type=submit]");
  let opener;
  let timer;
  let searchNumber = 0;

  function clearSearch() {
    clearTimeout(timer);
    searchNumber += 1;
    debater.value = "";
    submit.disabled = true;
    results.replaceChildren();
    status.textContent = "";
  }

  function addOption(match) {
    const option = document.createElement("button");
    option.type = "button";
    option.className = "list-group-item list-group-item-action";
    option.textContent = match.school
      ? `${match.name} · ${match.school}`
      : match.name;
    option.addEventListener("click", () => {
      clearSearch();
      debater.value = match.id;
      input.value = match.name;
      status.textContent = `Selected ${option.textContent}`;
      submit.disabled = false;
      submit.focus();
    });
    results.append(option);
  }

  async function loadResults(query, number) {
    const url = new URL(form.dataset.searchUrl, window.location.href);
    url.searchParams.set("q", query);
    try {
      const response = await fetch(url);
      if (!response.ok || response.redirected) throw new Error("Search failed");
      const data = await response.json();
      if (number !== searchNumber) return;
      data.results.forEach(addOption);
      status.textContent = data.results.length
        ? ""
        : "No debaters match that name.";
    } catch {
      if (number === searchNumber) {
        status.textContent = "Search is unavailable. Try again.";
      }
    }
  }

  grid.querySelectorAll("[data-cell]").forEach((button) => {
    button.addEventListener("click", () => {
      opener = button;
      cell.value = button.dataset.cell;
      clue.textContent = button.dataset.clue;
      input.value = "";
      clearSearch();
      modal.showModal();
      input.focus();
    });
    button.removeAttribute("disabled");
  });
  modal
    .querySelector("[data-close-picker]")
    .addEventListener("click", () => modal.close());
  modal.addEventListener("close", () => {
    clearSearch();
    if (opener) opener.focus();
  });
  input.addEventListener("input", () => {
    clearSearch();
    const query = input.value.trim();
    if (query.length < 2) return;
    const number = searchNumber;
    timer = setTimeout(() => loadResults(query, number), 180);
  });
  form.addEventListener("submit", (event) => {
    if (!debater.value || submit.disabled) {
      event.preventDefault();
      return;
    }
    submit.disabled = true;
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initImmaculateGrid, {
    once: true,
  });
} else {
  initImmaculateGrid();
}
