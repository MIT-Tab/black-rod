import Papa from "papaparse";
import blossom from "edmonds-blossom";

// eslint-disable-next-line no-restricted-globals
const workerScope = self;
let workerState = null;

function parseCsv(csvText) {
  const parsed = Papa.parse(csvText, {
    skipEmptyLines: false,
  });
  if (parsed.errors && parsed.errors.length > 0) {
    throw new Error(parsed.errors[0].message);
  }
  return parsed.data;
}

function parseOptionalInt(rawValue, fieldName, rowLabel) {
  if (rawValue === "") {
    return null;
  }

  const value = Number.parseInt(rawValue, 10);
  if (Number.isNaN(value)) {
    throw new Error(`${fieldName} for ${rowLabel} must be a whole number.`);
  }
  return value;
}

function splitTags(rawValue) {
  if (!rawValue) {
    return [];
  }
  return rawValue
    .split(",")
    .map((tag) => tag.trim())
    .filter(Boolean);
}

function parseDatesCsv(datesCsvText) {
  const rows = parseCsv(datesCsvText);
  if (rows.length < 2) {
    throw new Error(
      "Dates CSV must include a header row and at least one date.",
    );
  }

  const dates = {};
  rows.slice(1).forEach((row) => {
    const dateValue = (row[0] || "").trim();
    if (!dateValue) {
      return;
    }

    const tournamentsValue = (row[1] || "").trim();
    const tagValue = (row[2] || "").trim();
    let numTournaments = 0;
    if (tournamentsValue.includes("-")) {
      numTournaments = [1, 2];
    } else if (tournamentsValue !== "") {
      numTournaments = Number.parseInt(tournamentsValue, 10);
      if (Number.isNaN(numTournaments)) {
        throw new Error(
          `Number of tournaments for ${dateValue} must be a whole number or 1-2.`,
        );
      }
    }

    const tags = splitTags(tagValue);
    let unopposed = false;
    const unopposedIndex = tags.indexOf("Unopposed");
    if (unopposedIndex >= 0) {
      tags.splice(unopposedIndex, 1);
      unopposed = true;
    }

    const month = dateValue.split("/")[0];
    dates[dateValue] = {
      date: dateValue,
      num_tournaments: numTournaments,
      tags,
      schools: {},
      region: null,
      unopposed,
      sem: ["8", "9", "10", "11", "12"].includes(month) ? 1 : 2,
    };
  });

  if (Object.keys(dates).length === 0) {
    throw new Error("Dates CSV did not contain any schedule dates.");
  }

  return dates;
}

function parseSchoolsCsv(schoolsCsvText, knownDates) {
  const rows = parseCsv(schoolsCsvText);
  if (rows.length < 4) {
    throw new Error(
      "Schools CSV must include the two heading rows, the label row, and at least one school.",
    );
  }

  const dateIndexes = {};
  rows[0].forEach((value, index) => {
    const trimmed = (value || "").trim();
    if (trimmed) {
      dateIndexes[index] = trimmed;
    }
  });

  const unknownDates = Object.values(dateIndexes).filter(
    (dateValue) => !knownDates.includes(dateValue),
  );
  if (unknownDates.length > 0) {
    throw new Error(
      `Schools CSV includes dates that are missing from the dates CSV: ${unknownDates.join(", ")}`,
    );
  }

  const schools = [];
  rows.slice(3).forEach((row) => {
    const schoolName = (row[0] || "").trim();
    if (!schoolName) {
      return;
    }

    const availability = {};
    Object.entries(dateIndexes).forEach(([index, dateValue]) => {
      availability[dateValue] = (row[index] || "").trim();
    });

    schools.push({
      name: schoolName,
      region: (row[1] || "").trim(),
      desired_tournaments: parseOptionalInt(
        (row[2] || "").trim(),
        "Desired tournaments",
        schoolName,
      ),
      priority: parseOptionalInt((row[3] || "").trim(), "Priority", schoolName),
      tags: splitTags((row[4] || "").trim().replace(/"/g, "")),
      availability,
      semester: "any",
    });
  });

  if (schools.length === 0) {
    throw new Error("Schools CSV did not contain any school rows.");
  }

  return schools;
}

function loadSchedulerInputs(schoolsCsvText, datesCsvText) {
  const datesLookup = parseDatesCsv(datesCsvText);
  const schools = parseSchoolsCsv(schoolsCsvText, Object.keys(datesLookup));

  schools.forEach((school) => {
    Object.entries(school.availability).forEach(([dateValue, status]) => {
      if (status) {
        datesLookup[dateValue].schools[school.name] = status;
      }
    });
  });

  return { schools, datesLookup };
}

function getRankPenalties(settings) {
  return {
    "Already Scheduled": settings.already_scheduled_penalty,
    "Rank 1": settings.rank_1_penalty,
    "Rank 2": settings.rank_2_penalty,
    "Rank 3": settings.rank_3_penalty,
    Impossible: settings.impossible_penalty,
  };
}

function getRegionPenalties(settings) {
  return {
    "North|South": settings.north_to_south_penalty,
    "North|Central": settings.north_to_central_penalty,
    "South|Central": settings.south_to_central_penalty,
    "Central|North": settings.central_to_north_penalty,
    "Central|South": settings.central_to_south_penalty,
    "South|North": settings.south_to_north_penalty,
  };
}

function getPenalty(school, date, settings) {
  const rankPenalties = getRankPenalties(settings);
  const regionPenalties = getRegionPenalties(settings);
  const details = {};
  let penalty = 10000;

  const preferenceValue = school.availability[date.date] || "Impossible";
  if (!(preferenceValue in rankPenalties)) {
    throw new Error(
      `${school.name} has an unsupported preference value for ${date.date}: ${preferenceValue}.`,
    );
  }

  const priority = school.priority || 1;
  const rankPenalty = rankPenalties[preferenceValue] * priority;
  penalty += rankPenalty;
  if (preferenceValue !== "Already Scheduled") {
    details.preference = rankPenalty;
  }

  const regionKey = `${school.region}|${date.region}`;
  if (
    school.region === "Central" &&
    ["North", "South"].includes(date.region) &&
    date.active_tournament_count === 2
  ) {
    penalty += settings.central_on_two_tournament_weekend_penalty;
    details.region = settings.central_on_two_tournament_weekend_penalty;
  } else if (regionKey in regionPenalties) {
    penalty += regionPenalties[regionKey];
    details.region = regionPenalties[regionKey];
  }

  if (date.tags.length > 0) {
    const missingTags = date.tags.filter((tag) => !school.tags.includes(tag));
    if (missingTags.length > 0) {
      penalty += settings.missing_tag_penalty;
      details[`missing tags: ${missingTags.join(", ")}`] =
        settings.missing_tag_penalty;
    } else {
      penalty += settings.tag_bonus;
    }
  }

  if (date.unopposed && !school.tags.includes("Unopposed")) {
    penalty += settings.missing_unopposed_host_penalty;
    details["no unopposed host"] = settings.missing_unopposed_host_penalty;
  }

  if (!date.unopposed && school.tags.includes("Unopposed")) {
    const requestedPenalty =
      settings.missing_requested_unopposed_penalty * priority;
    penalty += requestedPenalty;
    details["missing requested unopposed"] = requestedPenalty;
  }

  return { penalty, details };
}

function cloneSchool(school) {
  return {
    ...school,
    availability: { ...school.availability },
    tags: [...school.tags],
  };
}

function cloneDate(date) {
  return {
    ...date,
    tags: [...date.tags],
    schools: { ...date.schools },
  };
}

function buildNodes(schools, dates, seed) {
  const schoolNodes = [];
  const dateNodes = [];

  schools.forEach((school) => {
    if (school.desired_tournaments === 1) {
      schoolNodes.push(cloneSchool(school));
    } else if (school.desired_tournaments === 2) {
      const semesterOne = cloneSchool(school);
      const semesterTwo = cloneSchool(school);
      semesterOne.semester = 1;
      semesterTwo.semester = 2;
      schoolNodes.push(semesterOne, semesterTwo);
    }
  });

  let oneToTwoIndex = 0;
  dates.forEach((date) => {
    let numTournaments = date.num_tournaments;
    if (Array.isArray(numTournaments)) {
      numTournaments = numTournaments[(seed >> oneToTwoIndex) & 1];
      oneToTwoIndex += 1;
    }

    if (numTournaments === 1) {
      const unopposed = cloneDate(date);
      unopposed.unopposed = true;
      unopposed.active_tournament_count = 1;
      dateNodes.push(unopposed);
    } else if (numTournaments === 2) {
      const north = cloneDate(date);
      const south = cloneDate(date);
      north.region = "North";
      south.region = "South";
      north.active_tournament_count = 2;
      south.active_tournament_count = 2;
      dateNodes.push(north, south);
    } else if (numTournaments === 3) {
      const north = cloneDate(date);
      const south = cloneDate(date);
      const central = cloneDate(date);
      north.region = "North";
      south.region = "South";
      central.region = "Central";
      north.active_tournament_count = 3;
      south.active_tournament_count = 3;
      central.active_tournament_count = 3;
      dateNodes.push(north, south, central);
    }
  });

  return { schoolNodes, dateNodes };
}

function buildMatching(schools, dates, seed, settings) {
  const { schoolNodes, dateNodes } = buildNodes(schools, dates, seed);
  const edges = [];
  const penaltyLookup = {};

  dateNodes.forEach((date, dateIndex) => {
    const vertexIndex = dateIndex + schoolNodes.length;
    schoolNodes.forEach((school, schoolIndex) => {
      if (school.semester !== "any" && school.semester !== date.sem) {
        return;
      }

      const { penalty, details } = getPenalty(school, date, settings);
      edges.push([schoolIndex, vertexIndex, penalty]);
      penaltyLookup[`${schoolIndex}|${vertexIndex}`] = {
        penalty,
        details,
      };
    });
  });

  if (edges.length === 0) {
    throw new Error(
      "The uploaded inputs do not produce any valid scheduling edges.",
    );
  }

  const matching = blossom(edges, true);
  const schedule = {};
  const matchedSchoolIndexes = new Set();
  let totalPenalty = 0;
  const nodes = schoolNodes.concat(dateNodes);

  matching.forEach((match, index) => {
    if (match === -1 || match < index) {
      return;
    }
    if (index >= schoolNodes.length || match < schoolNodes.length) {
      return;
    }

    const school = nodes[index];
    const date = nodes[match];
    const { penalty, details } = penaltyLookup[`${index}|${match}`];
    matchedSchoolIndexes.add(index);
    totalPenalty += penalty;

    if (!schedule[date.date]) {
      schedule[date.date] = [];
    }
    schedule[date.date].push({
      weekend_count: date.active_tournament_count || date.num_tournaments,
      school: school.name,
      region: date.region,
      preference: school.availability[date.date] || "",
      weight: penalty,
      penalties: details,
    });
  });

  const unmatchedSchools = [];
  schoolNodes.forEach((school, index) => {
    if (!matchedSchoolIndexes.has(index)) {
      unmatchedSchools.push(school.name);
    }
  });

  return { schedule, totalPenalty, unmatchedSchools };
}

function sortKey(dateValue) {
  const month = Number.parseInt(dateValue.split("/")[0], 10);
  const day = Number.parseInt(dateValue.split("/")[1].split("-")[0], 10);
  const academicYear = month >= 8 ? 0 : 1;
  return [academicYear, month, day];
}

function compareScheduleDate(a, b) {
  const left = sortKey(a);
  const right = sortKey(b);
  for (let index = 0; index < left.length; index += 1) {
    if (left[index] !== right[index]) {
      return left[index] - right[index];
    }
  }
  return 0;
}

function makeOutputText(schedule, datesLookup, bestPenalty, bestSeed) {
  const lines = [`Total Penalty: ${bestPenalty} Seed: ${bestSeed}`];
  Object.keys(schedule)
    .sort(compareScheduleDate)
    .forEach((dateValue) => {
      const assignments = schedule[dateValue];
      const weekendCount =
        assignments.length > 0 ? assignments[0].weekend_count : 0;
      lines.push("");
      lines.push(`${dateValue} (${weekendCount}):`);
      assignments.forEach((assignment) => {
        const tags = [...datesLookup[dateValue].tags];
        if (
          Array.isArray(datesLookup[dateValue].num_tournaments) &&
          assignments.length === 1
        ) {
          tags.push("Unopposed");
        }
        const tagText = tags.length > 0 ? ` (${tags.join(", ")})` : "";
        const penaltyText = Object.entries(assignment.penalties)
          .map(([key, value]) => `${key}: ${value}`)
          .join(", ");
        lines.push(`${assignment.school}${tagText}: [${penaltyText}]`);
      });
    });
  return lines.join("\n");
}

function serializeSchedule(bestSchedule, datesLookup) {
  return Object.keys(bestSchedule)
    .sort(compareScheduleDate)
    .map((dateValue) => {
      const assignments = [...bestSchedule[dateValue]].sort((left, right) => {
        const regionCompare = (left.region || "").localeCompare(
          right.region || "",
        );
        if (regionCompare !== 0) {
          return regionCompare;
        }
        return left.school.localeCompare(right.school);
      });
      const tags = [...datesLookup[dateValue].tags];
      if (
        Array.isArray(datesLookup[dateValue].num_tournaments) &&
        assignments.length === 1
      ) {
        tags.push("Unopposed");
      }
      return {
        date: dateValue,
        weekend_count:
          assignments.length > 0 ? assignments[0].weekend_count : 0,
        tags,
        assignments: assignments.map((assignment) => ({
          ...assignment,
          penalties_text: Object.entries(assignment.penalties)
            .map(([key, value]) => `${key}: ${value}`)
            .join(", "),
        })),
      };
    });
}

function runSeedBatch(startSeed, endSeed) {
  const { schools, datesLookup, settings } = workerState;
  const dates = Object.values(datesLookup);
  let bestResult = null;

  for (let seed = startSeed; seed < endSeed; seed += 1) {
    const { schedule, totalPenalty, unmatchedSchools } = buildMatching(
      schools,
      dates,
      seed,
      settings,
    );

    if (!bestResult || totalPenalty > bestResult.best_penalty) {
      bestResult = {
        best_seed: seed,
        best_penalty: totalPenalty,
        unmatched_schools: unmatchedSchools,
        schedule: serializeSchedule(schedule, datesLookup),
        output_text: makeOutputText(schedule, datesLookup, totalPenalty, seed),
        summary: {
          school_count: schools.length,
          date_count: Object.keys(datesLookup).length,
          scheduled_dates: Object.keys(schedule).length,
          unmatched_school_count: unmatchedSchools.length,
        },
      };
    }
  }

  return {
    completedSeeds: endSeed - startSeed,
    bestResult,
  };
}

workerScope.onmessage = (event) => {
  const { type, payload } = event.data;

  try {
    if (type === "init") {
      const { schools, datesLookup } = loadSchedulerInputs(
        payload.schoolsCsv,
        payload.datesCsv,
      );
      workerState = {
        schools,
        datesLookup,
        settings: payload.settings,
      };
      workerScope.postMessage({ type: "initialized" });
      return;
    }

    if (type === "runBatch") {
      if (!workerState) {
        throw new Error("Worker state is not initialized.");
      }
      const batchResult = runSeedBatch(payload.startSeed, payload.endSeed);
      workerScope.postMessage({
        type: "batchComplete",
        payload: {
          startSeed: payload.startSeed,
          endSeed: payload.endSeed,
          ...batchResult,
        },
      });
    }
  } catch (error) {
    workerScope.postMessage({
      type: "error",
      error: error && error.message ? error.message : String(error),
    });
  }
};
