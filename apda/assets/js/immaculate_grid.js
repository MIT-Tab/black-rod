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

function renderMissNames(list, names) {
  list.replaceChildren();
  names.forEach((name) => {
    const item = document.createElement("li");
    item.className = "list-group-item font-weight-bold";
    item.textContent = name;
    list.append(item);
  });
}

function renderMissTable(table, misses, clues) {
  const headers = table.querySelectorAll("[data-miss-clue]");
  clues.forEach((clue, index) => {
    headers[index].textContent = clue;
  });
  table.tBodies[0]?.remove();
  const body = table.createTBody();
  misses.forEach((miss) => {
    const row = body.insertRow();
    const name = document.createElement("th");
    name.scope = "row";
    name.textContent = miss.name;
    row.append(name);
    miss.matches.forEach((matched, index) => {
      const cell = row.insertCell();
      const result = matched ? "Yes" : "No";
      cell.textContent = matched === null ? "Unknown" : result;
      const detail = miss.details?.[index];
      if (detail) {
        const note = document.createElement("small");
        note.className = "d-block text-muted";
        note.textContent = detail;
        cell.append(note);
      }
    });
  });
}

function initAnswerReview(grid, missNames) {
  const modal = grid.querySelector("#immaculate-answers");
  if (!modal) return;

  const select = modal.querySelector("#immaculate-answer-cell");
  const results = modal.querySelector("[data-answer-results]");
  const status = modal.querySelector("[data-answer-status]");
  const clue = modal.querySelector("[data-answer-clue]");
  const missResults = modal.querySelector("[data-miss-results]");
  const missTable = modal.querySelector("[data-miss-table]");
  const missNote = modal.querySelector("[data-miss-note]");
  const hasAnswerKey = modal.hasAttribute("data-answer-key");
  let opener;
  let activeRequest;

  async function loadAnswers() {
    clue.textContent = select.selectedOptions[0].dataset.clue;
    const misses = missNames[select.value] ?? [];
    clue.hidden = false;
    missResults.hidden = false;
    if (missTable) missTable.hidden = true;
    renderMissNames(missResults, misses);
    missNote.textContent = misses.length ? "" : "No incorrect guesses.";
    if (!hasAnswerKey) return;
    if (activeRequest) activeRequest.abort();
    const request = new AbortController();
    activeRequest = request;
    results.replaceChildren();
    status.textContent = "Loading answers…";
    const url = new URL(grid.dataset.answersUrl, window.location.href);
    url.searchParams.set("day", grid.dataset.day);
    url.searchParams.set("board", grid.dataset.board);
    url.searchParams.set("cell", select.value);
    try {
      const response = await fetch(url, { signal: request.signal });
      if (!response.ok || response.redirected)
        throw new Error("Answers unavailable");
      const data = await response.json();
      if (activeRequest !== request) return;
      if (data.misses.length) {
        renderMissTable(missTable, data.misses, data.clues);
        missTable.hidden = false;
        missResults.hidden = true;
        clue.hidden = true;
        missNote.textContent = data.approximate
          ? "Clue matches use the tournament records available now."
          : "";
      } else {
        renderMissNames(missResults, []);
        missNote.textContent = "No incorrect guesses.";
      }
      data.answers.forEach((answer) => {
        const item = document.createElement("li");
        item.className =
          "list-group-item d-flex align-items-center justify-content-between";
        const profile = document.createElement("span");
        profile.className = "immaculate-answer-profile";
        const name = document.createElement("strong");
        name.textContent = answer.name;
        const school = document.createElement("small");
        school.className = "d-block text-muted";
        school.textContent = answer.school;
        profile.append(name, school);
        const score = document.createElement("span");
        score.className = `immaculate-answer-score immaculate-rarity-${answer.tier.key}`;
        score.textContent = `${answer.tier.label} · ${answer.score} pts`;
        item.append(profile, score);
        results.append(item);
      });
      status.textContent = `${data.answers.length} valid answers`;
    } catch {
      if (activeRequest === request && !request.signal.aborted) {
        status.textContent =
          "Answers are unavailable. Choose the square again to retry.";
      }
    }
  }

  grid.querySelectorAll("[data-review-cell]").forEach((button) => {
    button.addEventListener("click", () => {
      opener = button;
      select.value = button.dataset.reviewCell;
      modal.showModal();
      select.focus();
      loadAnswers();
    });
    button.removeAttribute("disabled");
  });
  select.addEventListener("change", loadAnswers);
  modal
    .querySelector("[data-close-answers]")
    .addEventListener("click", () => modal.close());
  modal.addEventListener("close", () => {
    if (activeRequest) activeRequest.abort();
    if (opener) opener.focus();
  });
}

function initImmaculateGrid() {
  const grid = document.querySelector("[data-immaculate-grid]");
  if (!grid) return;
  const missNames = JSON.parse(
    grid.querySelector("#immaculate-miss-names")?.textContent ?? "{}",
  );
  initResultsSharing(grid);
  initAnswerReview(grid, missNames);

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
  const pickerMisses = modal.querySelector("[data-picker-misses]");
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
      data.results.forEach((match) =>
        knownMatches.set(match.person ?? match.id, match),
      );
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
      const misses = missNames[cell.value] ?? [];
      pickerMisses.hidden = misses.length === 0;
      renderMissNames(
        pickerMisses.querySelector("[data-miss-results]"),
        misses,
      );
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
