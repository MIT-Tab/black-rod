function initResultsSharing(grid) {
  const button = grid.querySelector("[data-copy-results]");
  if (!button) return;

  const text = grid.querySelector("#immaculate-share-results");
  const status = grid.querySelector("[data-copy-status]");
  const fallback = grid.querySelector("[data-share-fallback]");
  const playUrl = new URL(grid.dataset.playUrl, window.location.href);
  text.value = `${text.value}\n${playUrl.href}`;
  button.removeAttribute("disabled");
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text.value);
      status.textContent = "Results copied.";
    } catch {
      fallback.hidden = false;
      text.focus();
      text.select();
      status.textContent = "Select and copy your results below.";
    }
  });
}

function initImmaculateGrid() {
  const grid = document.querySelector("[data-immaculate-grid]");
  if (!grid) return;
  initResultsSharing(grid);

  const modal = grid.querySelector("#immaculate-guess");
  if (!modal) return;

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
  let activeRequest;
  const knownMatches = new Map();

  function clearSearch() {
    clearTimeout(timer);
    if (activeRequest) activeRequest.abort();
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

  function previousMatches(query) {
    const words = query.toLocaleLowerCase().split(/\s+/);
    return [...knownMatches.values()]
      .filter((match) =>
        words.every((word) => match.name.toLocaleLowerCase().includes(word)),
      )
      .slice(0, 12);
  }

  async function loadResults(query, number) {
    const url = new URL(form.dataset.searchUrl, window.location.href);
    url.searchParams.set("q", query);
    const request = new AbortController();
    activeRequest = request;
    try {
      const response = await fetch(url, { signal: request.signal });
      if (!response.ok || response.redirected) throw new Error("Search failed");
      const data = await response.json();
      if (number !== searchNumber) return;
      data.results.forEach((match) => knownMatches.set(match.id, match));
      const matches = data.results.length
        ? data.results
        : previousMatches(query);
      results.replaceChildren();
      matches.forEach(addOption);
      status.textContent = matches.length ? "" : "No debaters match that name.";
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
    const query = input.value.trim().replace(/\s+/g, " ");
    if (query.length < 2) return;
    previousMatches(query).forEach(addOption);
    status.textContent = "Searching…";
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
