import csv
from dataclasses import dataclass, field
from io import StringIO


class SchedulerDataError(ValueError):
    pass


def _cell(row, index):
    return row[index].strip() if index < len(row) and row[index] is not None else ""


def _parse_optional_int(raw_value, field_name, row_label):
    if raw_value == "":
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError) as exc:
        raise SchedulerDataError(
            f"{field_name} for {row_label} must be a whole number."
        ) from exc


def _split_tags(raw_value):
    if not raw_value:
        return []
    return [tag.strip() for tag in raw_value.split(",") if tag.strip()]


@dataclass
class SchedulerSchool:
    name: str
    desired_tournaments: int | None
    priority: int | None
    availability: dict
    region: str
    tags: list[str]
    semester: int | str = "any"


@dataclass
class SchedulerDate:
    date: str
    num_tournaments: int | list[int]
    tags: list[str]
    schools: dict = field(default_factory=dict)
    region: str | None = None
    unopposed: bool = False
    sem: int = 1
    active_tournament_count: int | None = None


def parse_dates_csv(dates_csv_text):
    rows = list(csv.reader(StringIO(dates_csv_text)))
    if len(rows) < 2:
        raise SchedulerDataError("Dates CSV must include a header row and at least one date.")

    dates = {}
    for row in rows[1:]:
        date_value = _cell(row, 0)
        if not date_value:
            continue
        tournaments_value = _cell(row, 1)
        tag_value = _cell(row, 2)
        if "-" in tournaments_value:
            num_tournaments = [1, 2]
        elif tournaments_value == "":
            num_tournaments = 0
        else:
            try:
                num_tournaments = int(tournaments_value)
            except ValueError as exc:
                raise SchedulerDataError(
                    f"Number of tournaments for {date_value} must be a whole number or 1-2."
                ) from exc

        tags = _split_tags(tag_value)
        unopposed = False
        if "Unopposed" in tags:
            tags.remove("Unopposed")
            unopposed = True
        month = date_value.split("/")[0]
        dates[date_value] = SchedulerDate(
            date=date_value,
            num_tournaments=num_tournaments,
            tags=tags,
            schools={},
            region=None,
            unopposed=unopposed,
            sem=1 if month in {"8", "9", "10", "11", "12"} else 2,
        )

    if not dates:
        raise SchedulerDataError("Dates CSV did not contain any schedule dates.")
    return dates


def parse_schools_csv(schools_csv_text, known_dates):
    rows = list(csv.reader(StringIO(schools_csv_text)))
    if len(rows) < 3:
        raise SchedulerDataError(
            "Schools CSV must include the date row, the label row, and at least one school."
        )

    date_indexes = {}
    for index, raw_value in enumerate(rows[0]):
        value = raw_value.strip()
        if value:
            date_indexes[index] = value

    unknown_dates = sorted(set(date_indexes.values()) - set(known_dates))
    if unknown_dates:
        raise SchedulerDataError(
            "Schools CSV includes dates that are missing from the dates CSV: "
            + ", ".join(unknown_dates)
        )

    label_row_index = 1
    data_start_index = 2
    second_row = [_cell(rows[1], index) for index in range(len(rows[1]))]
    if any(value == "# of Tournaments" for value in second_row):
        label_row_index = 2
        data_start_index = 3

    if len(rows) <= data_start_index:
        raise SchedulerDataError("Schools CSV did not contain any school rows.")

    label_row = [_cell(rows[label_row_index], index) for index in range(len(rows[label_row_index]))]
    if not label_row or not label_row[0].startswith("School Name"):
        raise SchedulerDataError(
            "Schools CSV must include a label row starting with 'School Name'."
        )

    schools = []
    for row in rows[data_start_index:]:
        school_name = _cell(row, 0)
        if not school_name:
            continue
        availability = {
            date_value: _cell(row, index)
            for index, date_value in date_indexes.items()
            if date_value in known_dates
        }
        school = SchedulerSchool(
            name=school_name,
            region=_cell(row, 1),
            desired_tournaments=_parse_optional_int(
                _cell(row, 2),
                "Desired tournaments",
                school_name,
            ),
            priority=_parse_optional_int(_cell(row, 3), "Priority", school_name),
            tags=_split_tags(_cell(row, 4).replace('"', "")),
            availability=availability,
        )
        schools.append(school)

    if not schools:
        raise SchedulerDataError("Schools CSV did not contain any school rows.")
    return schools


def load_scheduler_inputs(schools_csv_text, dates_csv_text):
    dates = parse_dates_csv(dates_csv_text)
    schools = parse_schools_csv(schools_csv_text, dates.keys())

    for school in schools:
        for date_value, status in school.availability.items():
            if status:
                dates[date_value].schools[school.name] = status

    return schools, dates


def count_scheduler_scenarios(dates):
    flexible_dates = [
        date for date in dates.values() if isinstance(date.num_tournaments, list)
    ]
    return {
        "flexible_date_count": len(flexible_dates),
        "scenario_count": 2 ** len(flexible_dates),
    }


def summarize_scheduler_inputs(schools_csv_text, dates_csv_text):
    schools, dates = load_scheduler_inputs(schools_csv_text, dates_csv_text)
    active_dates = [date for date in dates.values() if date.num_tournaments]
    scenario_info = count_scheduler_scenarios(dates)
    tags = sorted({tag for school in schools for tag in school.tags})
    return {
        "school_count": len(schools),
        "date_count": len(dates),
        "active_date_count": len(active_dates),
        "flexible_date_count": scenario_info["flexible_date_count"],
        "scenario_count": scenario_info["scenario_count"],
        "preview_schools": schools,
        "preview_dates": list(dates.values()),
        "tags": tags,
    }
