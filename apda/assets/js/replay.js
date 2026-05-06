const DAY_MS = 24 * 60 * 60 * 1000;
const SPEED_PRESETS = {
  slow: 1000,
  medium: 500,
  fast: 250,
  turbo: 120,
};
const DEFAULT_SPEED_KEY = "fast";

const formatPoints = (value) => {
  if (value === null || value === undefined) {
    return "0";
  }
  const numericValue = Number(value);
  if (Number.isNaN(numericValue)) {
    return "0";
  }
  const rounded = Math.round((numericValue + Number.EPSILON) * 100) / 100;
  if (Math.abs(rounded - Math.round(rounded)) < 0.001) {
    return Math.round(rounded).toString();
  }
  return rounded.toFixed(2).replace(/\.?0+$/, "");
};

const escapeHtml = (value) => {
  if (!value) {
    return "";
  }
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
};

const formatDateLabel = (isoDate) => {
  if (!isoDate) {
    return "Season start";
  }
  const safeDate = new Date(`${isoDate}T12:00:00Z`);
  if (Number.isNaN(safeDate.getTime())) {
    return isoDate;
  }
  return safeDate.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
};

const shouldIncludeMarker = (marker, isoDate) => {
  if (!marker || !isoDate) {
    return false;
  }
  if (!marker.earned_on) {
    return true;
  }
  return marker.earned_on <= isoDate;
};

const sortMarkersForRanking = (markers) =>
  markers.slice().sort((a, b) => {
    if (b.points !== a.points) {
      return b.points - a.points;
    }
    const aDate = a.earned_on || "";
    const bDate = b.earned_on || "";
    if (aDate === bDate) {
      return (a.result_id || 0) - (b.result_id || 0);
    }
    return aDate.localeCompare(bDate);
  });

const assignPlaces = (rows) => {
  let currentPlace = 1;
  let lastPoints = null;
  rows.forEach((row, index) => {
    if (lastPoints !== null && Math.abs(row.points - lastPoints) < 0.001) {
      row.displayPlace = currentPlace;
      row.tied = true;
      if (index > 0) {
        rows[index - 1].tied = true;
        rows[index - 1].displayPlace = currentPlace;
      }
    } else {
      currentPlace = index + 1;
      row.displayPlace = currentPlace;
      row.tied = false;
    }
    lastPoints = row.points;
  });
};

const buildDebaterList = (debaters) => {
  if (!debaters || !debaters.length) {
    return "";
  }
  return debaters
    .map((debater) => {
      if (debater.url) {
        return `<a href="${debater.url}">${escapeHtml(debater.name)}</a>`;
      }
      return escapeHtml(debater.name);
    })
    .join(" & ");
};

const buildTeamCell = (team) => {
  if (!team) {
    return "";
  }
  const name = escapeHtml(team.name);
  if (team.url) {
    return `<a href="${team.url}">${name}</a>`;
  }
  return name;
};

const buildDebaterCell = (debater) => {
  if (!debater) {
    return "";
  }
  const name = escapeHtml(debater.name);
  if (debater.url) {
    return `<a href="${debater.url}">${name}</a>`;
  }
  return name;
};

const buildSchoolCell = (school) => {
  if (!school) {
    return "";
  }
  const name = escapeHtml(school.name);
  if (school.url) {
    return `<a href="${school.url}">${name}</a>`;
  }
  return name;
};

const buildMarkerCellContent = (marker) => {
  if (!marker) {
    return "";
  }
  const tournament = marker.tournament;
  const tournamentName = tournament
    ? escapeHtml(tournament.display || tournament.name || "")
    : "";
  const tournamentLink =
    tournament && tournament.url
      ? `<a href="${tournament.url}">${tournamentName}</a>`
      : tournamentName;
  return `${formatPoints(marker.points)}${
    tournamentName ? ` ${tournamentLink}` : ""
  }`;
};

const buildCotyRecentContent = (markers) => {
  const grouped = new Map();
  (markers || []).forEach((marker) => {
    const { tournament } = marker;
    const key = tournament ? tournament.id : "none";
    let current = grouped.get(key);
    if (!current) {
      current = {
        tournament,
        rawPoints: 0,
        qualBonus: 0,
        qualLabel: null,
      };
      grouped.set(key, current);
    }
    if (marker.marker_type === "qual_bonus") {
      current.qualBonus += Number(marker.points || 0);
      const label = marker.qualification && marker.qualification.label;
      if (label && label !== "Points") {
        current.qualLabel = label;
      } else if (!current.qualLabel) {
        current.qualLabel = "Qual";
      }
    } else if (marker.marker_type === "qual_points") {
      current.rawPoints += Number(
        marker.raw_points !== undefined
          ? marker.raw_points
          : marker.points || 0,
      );
    } else {
      current.rawPoints += Number(marker.points || 0);
    }
  });

  return Array.from(grouped.values())
    .map((entry) => {
      const { tournament } = entry;
      const tournamentName = tournament
        ? escapeHtml(tournament.display || tournament.name || "")
        : "";
      const tournamentLink =
        tournament && tournament.url
          ? `<a href="${tournament.url}">${tournamentName}</a>`
          : tournamentName;
      const parts = [];
      if (entry.rawPoints > 0) {
        parts.push(
          `+${formatPoints(entry.rawPoints)}${
            tournamentName ? ` ${tournamentLink}` : ""
          }`,
        );
      }
      if (entry.qualBonus > 0) {
        parts.push(
          `+${formatPoints(entry.qualBonus)} ${escapeHtml(
            entry.qualLabel || "Qual",
          )}`,
        );
      }
      return parts.join(" ");
    })
    .filter((html) => html.length > 0)
    .join("<br>");
};

const buildCotyDebaterBreakdown = (markers, highlightDate) => {
  const debaters = new Map();
  (markers || []).forEach((marker) => {
    if (!marker.debater || marker.debater.id === undefined) {
      return;
    }
    const key = marker.debater.id;
    const current = debaters.get(key) || {
      debater: marker.debater,
      rawPoints: 0,
      contribution: 0,
      qualled: false,
      markers: [],
      recentMarkers: [],
    };
    if (marker.marker_type === "qual_points") {
      current.rawPoints += Number(marker.raw_points ?? marker.points ?? 0);
    }
    current.contribution += Number(marker.points || 0);
    if (marker.marker_type === "qual_bonus") {
      current.qualled = true;
    }
    current.markers.push(marker);
    if (
      marker.earned_on &&
      highlightDate &&
      marker.earned_on === highlightDate
    ) {
      current.recentMarkers.push(marker);
    }
    debaters.set(key, current);
  });

  return Array.from(debaters.values()).sort((a, b) => {
    if (b.contribution !== a.contribution) {
      return b.contribution - a.contribution;
    }
    if (b.rawPoints !== a.rawPoints) {
      return b.rawPoints - a.rawPoints;
    }
    return (a.debater.name || "").localeCompare(b.debater.name || "");
  });
};

const buildCotyDebaterCell = (entry) => {
  const name = escapeHtml(entry.debater.name);
  const link = entry.debater.url
    ? `<a href="${entry.debater.url}">${name}</a>`
    : name;
  if (entry.qualled) {
    return `<b>${link} *</b>`;
  }
  return link;
};

const cotyDebaterSignature = (entry) => {
  const markerSig = (entry.markers || [])
    .map(
      (marker) =>
        `${marker.result_id || (marker.tournament && marker.tournament.id) || ""}:${
          marker.marker_type || ""
        }:${marker.points}`,
    )
    .join("|");
  return `${Math.round((entry.rawPoints || 0) * 1000) / 1000}|${
    Math.round((entry.contribution || 0) * 1000) / 1000
  }|${entry.qualled ? 1 : 0}|${markerSig}`;
};

const renderCotyRows = (
  target,
  row,
  highlightDate,
  collapsedSchools,
  debaterPrev,
  debaterNext,
) => {
  const placePrefix = row.tied ? "T-" : "";
  const markers = row.markers || [];
  const debaters = buildCotyDebaterBreakdown(markers, highlightDate);
  const schoolId = row.school && row.school.id;
  const isCollapsed =
    schoolId !== undefined &&
    schoolId !== null &&
    collapsedSchools &&
    collapsedSchools.has(schoolId);
  const summaryRow = document.createElement("tr");
  if (row.isNew) {
    summaryRow.classList.add("replay-row-enter");
  }
  const toggleAttr =
    schoolId !== undefined && schoolId !== null
      ? ` data-coty-toggle="${schoolId}"`
      : "";
  summaryRow.innerHTML = [
    `<td>${placePrefix}${row.displayPlace}</td>`,
    `<td>${buildSchoolCell(row.school)}</td>`,
    `<td>${formatPoints(row.points)}</td>`,
    `<td colspan="3"><a href="#" class="replay-coty-toggle"${toggleAttr}>Click to expand / collapse</a></td>`,
  ].join("");
  target.appendChild(summaryRow);

  debaters.forEach((entry, index) => {
    const tr = document.createElement("tr");
    if (schoolId !== undefined && schoolId !== null) {
      tr.setAttribute("data-coty-school-id", String(schoolId));
    }
    if (isCollapsed) {
      tr.classList.add("replay-coty-row-collapsed");
    }
    const debaterId = entry.debater && entry.debater.id;
    if (debaterId !== undefined && debaterId !== null && debaterNext) {
      const signature = cotyDebaterSignature(entry);
      const previousSig = debaterPrev ? debaterPrev.get(debaterId) : undefined;
      if (previousSig === undefined || previousSig !== signature) {
        tr.classList.add("replay-row-enter");
      }
      debaterNext.set(debaterId, signature);
    }
    const recent = buildCotyRecentContent(entry.recentMarkers);
    const recentClass = recent ? ' class="replay-marker-highlight"' : "";
    const cells = [];
    if (index === 0) {
      cells.push(`<td colspan="3" rowspan="${debaters.length}"></td>`);
    }
    cells.push(
      `<td class="short-row">${buildCotyDebaterCell(entry)}</td>`,
      `<td class="short-row">${formatPoints(entry.rawPoints)} (${formatPoints(
        entry.contribution,
      )})</td>`,
      `<td class="short-row"${recentClass}>${recent}</td>`,
    );
    tr.innerHTML = cells.join("");
    target.appendChild(tr);
  });
};

const renderRows = (
  tbody,
  rows,
  emptyMessage,
  markerSlots,
  highlightDate,
  collapsedSchools,
  debaterPrev,
  debaterNext,
) => {
  const target = tbody;
  target.innerHTML = "";
  if (!rows.length) {
    const emptyRow = document.createElement("tr");
    const colspan = markerSlots ? 4 + markerSlots : 6;
    emptyRow.innerHTML = `<td colspan="${colspan}" class="shaded text-center py-4">${escapeHtml(
      emptyMessage,
    )}</td>`;
    target.appendChild(emptyRow);
    return;
  }

  rows.forEach((row) => {
    const tr = document.createElement("tr");
    if (row.isNew) {
      tr.classList.add("replay-row-enter");
    }
    const placePrefix = row.tied ? "T-" : "";
    const markers = row.markers || [];
    const debaterListSource =
      row.type === "team" && row.team && row.team.debaters
        ? row.team.debaters
        : null;
    if (row.type === "school") {
      renderCotyRows(
        target,
        row,
        highlightDate,
        collapsedSchools,
        debaterPrev,
        debaterNext,
      );
      return;
    }
    const markerCells = Array.from({ length: markerSlots }).map((_, index) => {
      const marker = markers[index];
      const highlight =
        highlightDate &&
        marker &&
        marker.earned_on &&
        marker.earned_on === highlightDate;
      const cellClass = highlight ? ' class="replay-marker-highlight"' : "";
      return `<td${cellClass}>${buildMarkerCellContent(marker)}</td>`;
    });
    tr.innerHTML = [
      `<td>${placePrefix}${row.displayPlace}</td>`,
      row.type === "team"
        ? `<td>${buildTeamCell(row.team)}</td>`
        : `<td>${buildDebaterCell(row.debater)}</td>`,
      row.type === "team"
        ? `<td>${buildDebaterList(debaterListSource)}</td>`
        : `<td>${buildSchoolCell(row.school)}</td>`,
      `<td>${formatPoints(row.points)}</td>`,
      ...markerCells,
    ].join("");
    target.appendChild(tr);
  });
};

const entryNameForType = (entry, type) => {
  if (type === "team") {
    return (entry.team && entry.team.name) || "";
  }
  if (type === "school") {
    return (entry.school && entry.school.name) || "";
  }
  return (entry.debater && entry.debater.name) || "";
};

const entryKeyForType = (entry, type) => {
  if (type === "team") {
    return entry.team && entry.team.id;
  }
  if (type === "school") {
    return entry.school && entry.school.id;
  }
  return entry.debater && entry.debater.id;
};

const computeRows = (entries, isoDate, markerLimit, type) => {
  if (!isoDate) {
    return [];
  }
  const rows = [];
  entries.forEach((entry) => {
    const availableMarkers = (entry.all_markers || []).filter((marker) =>
      shouldIncludeMarker(marker, isoDate),
    );

    if (!availableMarkers.length) {
      return;
    }

    const sortedMarkers =
      type === "school"
        ? availableMarkers.slice().sort((a, b) => {
            const aDate = a.earned_on || "";
            const bDate = b.earned_on || "";
            if (aDate !== bDate) {
              return aDate.localeCompare(bDate);
            }
            if ((a.result_id || 0) !== (b.result_id || 0)) {
              return (a.result_id || 0) - (b.result_id || 0);
            }
            return (a.debater?.id || 0) - (b.debater?.id || 0);
          })
        : sortMarkersForRanking(availableMarkers);
    const bestMarkers = markerLimit
      ? sortedMarkers.slice(0, markerLimit)
      : sortedMarkers;
    const totalPoints = bestMarkers.reduce(
      (sum, marker) => sum + marker.points,
      0,
    );

    const entityName = entryNameForType(entry, type);
    const entityKey = entryKeyForType(entry, type);

    rows.push({
      type,
      team: entry.team,
      debater: entry.debater,
      school: entry.school,
      points: totalPoints,
      original_place: entry.place || 0,
      markers: bestMarkers,
      name: entityName,
      entityKey,
    });
  });

  rows.sort((a, b) => {
    if (b.points !== a.points) {
      return b.points - a.points;
    }
    if (a.original_place && b.original_place) {
      return a.original_place - b.original_place;
    }
    const nameA = (a.name || "").toLowerCase();
    const nameB = (b.name || "").toLowerCase();
    return nameA.localeCompare(nameB);
  });

  assignPlaces(rows);
  return rows;
};

const normalizeToDay = (value) => Math.round(value / DAY_MS) * DAY_MS;

const markerSignature = (markers) =>
  (markers || [])
    .map(
      (marker) =>
        `${marker.result_id || marker.tournament?.id || ""}:${marker.points}`,
    )
    .join("|");

const rowSignature = (row) => {
  const normalizedPoints = Math.round((row.points || 0) * 1000) / 1000;
  return `${normalizedPoints}|${markerSignature(row.markers)}`;
};

const snapshotRows = (rows) =>
  rows.map((row) => ({
    key: row.entityKey,
    signature: rowSignature(row),
  }));

const rowsHaveChanged = (previousSnapshot, newRows) => {
  if (!previousSnapshot) {
    return true;
  }
  if (previousSnapshot.length !== newRows.length) {
    return true;
  }
  for (let i = 0; i < newRows.length; i += 1) {
    const snapshot = previousSnapshot[i];
    const row = newRows[i];
    if ((snapshot.key || null) !== (row.entityKey || null)) {
      return true;
    }
    if (snapshot.signature !== rowSignature(row)) {
      return true;
    }
  }
  return false;
};

const initReplayPage = () => {
  const root = document.getElementById("standings-replay-root");
  if (!root) {
    return;
  }

  const slider = document.getElementById("replay-slider");
  const dateLabel = document.getElementById("replay-date-label");
  const playButton = document.getElementById("replay-play");
  const errorAlert = document.getElementById("replay-error");
  const totyBody = document.getElementById("replay-toty-body");
  const sotyBody = document.getElementById("replay-soty-body");
  const cotyBody = document.getElementById("replay-coty-body");
  const speedSelects = Array.from(
    document.querySelectorAll(".replay-speed-select"),
  );

  const syncSpeedSelects = (value) => {
    speedSelects.forEach((select) => {
      select.value = value;
    });
  };

  const state = {
    data: null,
    timeline: [],
    timelineMs: [],
    markerLimits: { toty: 5, soty: 6, coty: 0 },
    sliderRange: null,
    currentValue: null,
    playing: false,
    playTimer: null,
    playIntervalMs: SPEED_PRESETS[DEFAULT_SPEED_KEY],
    previousRows: { toty: new Map(), soty: new Map(), coty: new Map() },
    renderedRows: { toty: null, soty: null, coty: null },
    lastHighlight: { toty: null, soty: null, coty: null },
    speedKey: DEFAULT_SPEED_KEY,
    cotyCollapsed: new Set(),
    previousCotyDebaters: new Map(),
  };

  const updatePlayInterval = () => {
    state.playIntervalMs =
      SPEED_PRESETS[state.speedKey] || SPEED_PRESETS[DEFAULT_SPEED_KEY];
  };

  const setPlayIcon = (isPlaying) => {
    const icon = playButton.querySelector("i");
    if (!icon) {
      return;
    }
    if (isPlaying) {
      icon.classList.remove("fa-play");
      icon.classList.add("fa-pause");
    } else {
      icon.classList.remove("fa-pause");
      icon.classList.add("fa-play");
    }
  };

  const stopPlayback = () => {
    if (state.playTimer) {
      clearInterval(state.playTimer);
      state.playTimer = null;
    }
    state.playing = false;
    setPlayIcon(false);
  };

  const startPlayback = (resetPosition = true) => {
    if (!state.timeline.length || !state.sliderRange) {
      return;
    }

    if (state.currentValue === null) {
      state.currentValue = state.sliderRange.min;
      updateForSliderValue(state.currentValue);
    } else if (resetPosition && state.currentValue >= state.sliderRange.max) {
      state.currentValue = state.sliderRange.min;
      updateForSliderValue(state.currentValue);
    }

    stopPlayback();
    state.playing = true;
    updatePlayInterval();
    setPlayIcon(true);
    state.playTimer = setInterval(() => {
      if (state.currentValue >= state.sliderRange.max) {
        stopPlayback();
        return;
      }
      const nextValue = Math.min(
        state.currentValue + DAY_MS,
        state.sliderRange.max,
      );
      state.currentValue = nextValue;
      updateForSliderValue(state.currentValue);
    }, state.playIntervalMs);
  };

  const markNewRows = (type, rows) => {
    const previous = state.previousRows[type] || new Map();
    const nextMap = new Map();
    rows.forEach((row) => {
      if (row.entityKey === undefined || row.entityKey === null) {
        return;
      }
      const signature = rowSignature(row);
      if (
        !previous.has(row.entityKey) ||
        previous.get(row.entityKey) !== signature
      ) {
        row.isNew = true;
      }
      nextMap.set(row.entityKey, signature);
    });
    state.previousRows[type] = nextMap;
  };

  const updateTables = (isoDate) => {
    if (!state.data) {
      return;
    }
    const standings = state.data.standings || {};
    const totyRows = computeRows(
      standings.toty || [],
      isoDate,
      state.markerLimits.toty,
      "team",
    );
    const sotyRows = computeRows(
      standings.soty || [],
      isoDate,
      state.markerLimits.soty,
      "debater",
    );
    const cotyRows = computeRows(
      standings.coty || [],
      isoDate,
      state.markerLimits.coty,
      "school",
    );

    markNewRows("toty", totyRows);
    markNewRows("soty", sotyRows);
    markNewRows("coty", cotyRows);

    const totyRowsChanged = rowsHaveChanged(state.renderedRows.toty, totyRows);
    const sotyRowsChanged = rowsHaveChanged(state.renderedRows.soty, sotyRows);
    const cotyRowsChanged = rowsHaveChanged(state.renderedRows.coty, cotyRows);

    if (totyRowsChanged || state.lastHighlight.toty !== isoDate) {
      renderRows(
        totyBody,
        totyRows,
        "There are no TOTY results yet for this date.",
        state.markerLimits.toty,
        isoDate,
      );
      state.renderedRows.toty = snapshotRows(totyRows);
      state.lastHighlight.toty = isoDate;
    }
    if (sotyRowsChanged || state.lastHighlight.soty !== isoDate) {
      renderRows(
        sotyBody,
        sotyRows,
        "There are no SOTY results yet for this date.",
        state.markerLimits.soty,
        isoDate,
      );
      state.renderedRows.soty = snapshotRows(sotyRows);
      state.lastHighlight.soty = isoDate;
    }
    if (cotyBody && (cotyRowsChanged || state.lastHighlight.coty !== isoDate)) {
      const nextCotyDebaters = new Map();
      renderRows(
        cotyBody,
        cotyRows,
        "There are no COTY results yet for this date.",
        state.markerLimits.coty,
        isoDate,
        state.cotyCollapsed,
        state.previousCotyDebaters,
        nextCotyDebaters,
      );
      state.previousCotyDebaters = nextCotyDebaters;
      state.renderedRows.coty = snapshotRows(cotyRows);
      state.lastHighlight.coty = isoDate;
    }
  };

  const updateDateLabel = (sliderIso) => {
    if (!state.timeline.length || !sliderIso) {
      dateLabel.textContent = "No tournaments recorded yet.";
      return;
    }
    dateLabel.textContent = `Showing standings through ${formatDateLabel(
      sliderIso,
    )}`;
  };

  const findActiveTimelineDate = (value) => {
    if (!state.timeline.length) {
      return null;
    }
    const sliderIso = new Date(value).toISOString().slice(0, 10);
    let activeDate = null;
    for (let i = 0; i < state.timeline.length; i += 1) {
      const iso = state.timeline[i];
      if (iso <= sliderIso) {
        activeDate = iso;
      } else {
        break;
      }
    }
    return activeDate;
  };

  const updateForSliderValue = (value) => {
    if (!state.sliderRange) {
      return;
    }
    const clamped = Math.min(
      Math.max(value, state.sliderRange.min),
      state.sliderRange.max,
    );
    const normalized = normalizeToDay(clamped);
    state.currentValue = normalized;
    slider.value = normalized;
    const sliderIso = new Date(normalized).toISOString().slice(0, 10);
    const activeIso = findActiveTimelineDate(normalized);
    updateDateLabel(sliderIso);
    updateTables(activeIso);
  };

  const togglePlayback = () => {
    if (!state.timeline.length || !state.sliderRange) {
      return;
    }
    if (state.playing) {
      stopPlayback();
      return;
    }
    startPlayback(true);
  };

  const onSliderChange = (event) => {
    const value = Number(event.target.value);
    if (Number.isNaN(value)) {
      return;
    }
    if (state.playing) {
      stopPlayback();
    }
    updateForSliderValue(value);
  };

  const showError = (message) => {
    errorAlert.textContent = message;
    errorAlert.classList.remove("d-none");
  };

  const clearError = () => {
    errorAlert.textContent = "";
    errorAlert.classList.add("d-none");
  };

  const handleSpeedChange = (event) => {
    const { value } = event.target;
    if (!SPEED_PRESETS[value]) {
      syncSpeedSelects(state.speedKey);
      return;
    }
    state.speedKey = value;
    syncSpeedSelects(value);
    updatePlayInterval();
    if (state.playing) {
      startPlayback(false);
    }
  };

  slider.addEventListener("input", onSliderChange);
  slider.addEventListener("change", onSliderChange);
  playButton.addEventListener("click", togglePlayback);
  speedSelects.forEach((select) => {
    select.addEventListener("change", handleSpeedChange);
  });

  if (cotyBody) {
    cotyBody.addEventListener("click", (event) => {
      const toggle = event.target.closest("[data-coty-toggle]");
      if (!toggle || !cotyBody.contains(toggle)) {
        return;
      }
      event.preventDefault();
      const rawId = toggle.getAttribute("data-coty-toggle");
      const schoolId = Number(rawId);
      if (Number.isNaN(schoolId)) {
        return;
      }
      const willCollapse = !state.cotyCollapsed.has(schoolId);
      if (willCollapse) {
        state.cotyCollapsed.add(schoolId);
      } else {
        state.cotyCollapsed.delete(schoolId);
      }
      cotyBody
        .querySelectorAll(`[data-coty-school-id="${schoolId}"]`)
        .forEach((tr) => {
          tr.classList.toggle("replay-coty-row-collapsed", willCollapse);
        });
    });
  }

  const expandAllBtn = document.getElementById("replay-coty-expand-all");
  if (expandAllBtn && cotyBody) {
    expandAllBtn.addEventListener("click", (event) => {
      event.preventDefault();
      const schoolIds = new Set();
      cotyBody.querySelectorAll("[data-coty-school-id]").forEach((tr) => {
        const id = Number(tr.getAttribute("data-coty-school-id"));
        if (!Number.isNaN(id)) {
          schoolIds.add(id);
        }
      });
      if (!schoolIds.size) {
        return;
      }
      const anyExpanded = Array.from(schoolIds).some(
        (id) => !state.cotyCollapsed.has(id),
      );
      if (anyExpanded) {
        schoolIds.forEach((id) => state.cotyCollapsed.add(id));
      } else {
        schoolIds.forEach((id) => state.cotyCollapsed.delete(id));
      }
      cotyBody.querySelectorAll("[data-coty-school-id]").forEach((tr) => {
        tr.classList.toggle("replay-coty-row-collapsed", anyExpanded);
      });
    });
  }

  const apiUrl = root.getAttribute("data-api-url");
  if (!apiUrl) {
    showError("Replay API is not configured.");
    return;
  }

  fetch(apiUrl, { credentials: "same-origin" })
    .then((response) => {
      if (!response.ok) {
        throw new Error("Unable to load standings replay data.");
      }
      return response.json();
    })
    .then((data) => {
      clearError();
      state.data = data;
      state.markerLimits = data.marker_limits || state.markerLimits;
      state.timeline = (data.timeline_dates || []).slice().sort();
      state.timelineMs = state.timeline.map((iso) =>
        Date.parse(`${iso}T00:00:00Z`),
      );

      if (!state.timeline.length) {
        slider.disabled = true;
        playButton.disabled = true;
        updateDateLabel(null);
        updateTables(null);
        return;
      }

      const minTimeline = state.timelineMs[0];
      const maxTimeline = state.timelineMs[state.timelineMs.length - 1];
      const sliderMin = minTimeline - DAY_MS * 7;
      const sliderMax = maxTimeline;
      state.sliderRange = { min: sliderMin, max: sliderMax };
      slider.disabled = false;
      slider.min = sliderMin;
      slider.max = sliderMax;
      slider.step = DAY_MS;
      playButton.disabled = sliderMin === sliderMax;
      speedSelects.forEach((select) => {
        select.disabled = false;
      });
      syncSpeedSelects(state.speedKey);
      updatePlayInterval();
      state.currentValue = sliderMin;
      updateForSliderValue(state.currentValue);
    })
    .catch((error) => {
      showError(error.message);
      slider.disabled = true;
      playButton.disabled = true;
      totyBody.innerHTML =
        '<tr><td colspan="9" class="shaded text-center py-4">Unable to load standings.</td></tr>';
      sotyBody.innerHTML =
        '<tr><td colspan="10" class="shaded text-center py-4">Unable to load standings.</td></tr>';
      if (cotyBody) {
        cotyBody.innerHTML =
          '<tr><td colspan="6" class="shaded text-center py-4">Unable to load standings.</td></tr>';
      }
    });
};

document.addEventListener("DOMContentLoaded", initReplayPage);
