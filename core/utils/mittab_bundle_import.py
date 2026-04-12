import hashlib
import json
from dataclasses import dataclass

from django.db import transaction

from core.models import Debater, DebaterAlias, Round, School, TournamentImport
from core.utils.round_amendments import RoundAmendmentError, apply_round_amendments


class MittabBundleImportError(ValueError):
    pass


@dataclass(frozen=True)
class LoadedMittabBundle:
    document: dict
    source_hash: str
    original_file_name: str


def load_mittab_bundle(uploaded_file):
    try:
        raw_bytes = uploaded_file.read()
        decoded = raw_bytes.decode("utf-8-sig")
        document = json.loads(decoded)
    except UnicodeDecodeError as exc:
        raise MittabBundleImportError("Mit-Tab bundle files must be valid UTF-8 JSON.") from exc
    except json.JSONDecodeError as exc:
        raise MittabBundleImportError(f"Invalid JSON: {exc.msg}.") from exc

    return LoadedMittabBundle(
        document=document,
        source_hash=hashlib.sha256(raw_bytes).hexdigest(),
        original_file_name=str(getattr(uploaded_file, "name", "") or ""),
    )


def import_mittab_bundle(loaded_bundle, tournament, *, actor=None):
    document = loaded_bundle.document
    _validate_top_level_document(document)
    _ensure_results_exist(tournament)

    existing_duplicate = TournamentImport.objects.filter(
        tournament=tournament,
        import_type=TournamentImport.ImportType.MITTAB_BUNDLE,
        source_hash=loaded_bundle.source_hash,
    ).exists()
    if existing_duplicate:
        raise MittabBundleImportError(
            "This exact Mit-Tab bundle was already uploaded for the selected tournament."
        )

    with transaction.atomic():
        import_row = TournamentImport.objects.create(
            tournament=tournament,
            import_type=TournamentImport.ImportType.MITTAB_BUNDLE,
            original_file_name=loaded_bundle.original_file_name,
            source_hash=loaded_bundle.source_hash,
        )
        importer = _MittabBundleImporter(document, tournament, import_row)
        actions = importer.build_actions()
        try:
            summary = apply_round_amendments(
                {"actions": actions},
                actor=actor,
                source_context={
                    "source": "mittab_bundle_upload",
                    "file_name": loaded_bundle.original_file_name,
                    "import_id": import_row.id,
                },
            )
        except RoundAmendmentError as exc:
            raise MittabBundleImportError(str(exc)) from exc

    result = dict(summary)
    result["tournament_import_id"] = import_row.id
    return result


def _validate_top_level_document(document):
    if not isinstance(document, dict):
        raise MittabBundleImportError("Mit-Tab bundle JSON must be an object at the top level.")
    if int(document.get("schema_version") or 0) != 1:
        raise MittabBundleImportError("Unsupported Mit-Tab bundle schema version.")
    if str(document.get("source") or "").strip() != "mit_tab_black_rod_bundle":
        raise MittabBundleImportError("Uploaded JSON is not a supported Mit-Tab bundle.")
    if not isinstance(document.get("schools"), list):
        raise MittabBundleImportError("Mit-Tab bundle 'schools' must be a list.")
    if not isinstance(document.get("debaters"), list):
        raise MittabBundleImportError("Mit-Tab bundle 'debaters' must be a list.")
    rounds = document.get("rounds")
    if not isinstance(rounds, list) or not rounds:
        raise MittabBundleImportError("Mit-Tab bundle must include at least one round.")


def _ensure_results_exist(tournament):
    if tournament.team_results.exists() or tournament.speaker_results.exists():
        return
    raise MittabBundleImportError(
        "Results must be imported for this tournament before uploading a Mit-Tab bundle."
    )


class _MittabBundleImporter:
    def __init__(self, document, tournament, import_row):
        self.document = document
        self.tournament = tournament
        self.import_row = import_row
        self.schools_by_id = self._index_rows("schools")
        self.debaters_by_id = self._index_rows("debaters")

    def build_actions(self):
        import_keys = []
        actions = []

        for round_payload in self.document["rounds"]:
            action = self._build_round_action(round_payload)
            import_keys.append(action["import_key"])
            actions.append(action)

        stale_rounds = (
            Round.objects.filter(
                tournament=self.tournament,
                imported_metadata__sources__import_type=TournamentImport.ImportType.MITTAB_BUNDLE,
            )
            .exclude(import_key__in=import_keys)
            .distinct()
            .order_by("id")
        )
        for round_obj in stale_rounds:
            actions.append(
                {
                    "type": "delete_round",
                    "tournament_id": self.tournament.id,
                    "import_key": str(round_obj.import_key or ""),
                }
            )

        return actions

    def _build_round_action(self, round_payload):
        if not isinstance(round_payload, dict):
            raise MittabBundleImportError("Each round entry must be an object.")

        import_key = str(round_payload.get("import_key") or "").strip()
        if not import_key:
            raise MittabBundleImportError("Each round entry requires an import_key.")

        stage = str(round_payload.get("stage") or "").strip()
        if stage not in {Round.Stage.PRELIM, Round.Stage.OUTROUND}:
            raise MittabBundleImportError(f"Round '{import_key}' has an unsupported stage.")

        division = round_payload.get("division")
        if division not in (None, "", Round.Division.VARSITY, Round.Division.NOVICE):
            raise MittabBundleImportError(f"Round '{import_key}' has an unsupported division.")

        existing = Round.objects.filter(
            tournament=self.tournament,
            import_key=import_key,
        ).exists()
        action = {
            "type": "update_round" if existing else "create_round",
            "tournament_id": self.tournament.id,
            "import_key": import_key,
            "round_number": int(round_payload.get("round_number") or 0),
            "stage": stage,
            "division": None if division in (None, "") else str(division),
            "elim_size": round_payload.get("elim_size"),
            "round_label": str(round_payload.get("label") or ""),
            "victor": int(round_payload.get("victor") or 0),
            "import_origin": TournamentImport.ImportType.MITTAB_BUNDLE,
            "metadata": {
                "bundle_source": "mit_tab_black_rod_bundle",
                "schema_version": int(self.document.get("schema_version") or 0),
                "source_tournament_name": str(self.document.get("tournament_name") or ""),
                "bundle_exported_at": str(self.document.get("exported_at") or ""),
            },
            "stats": None,
        }

        gov_ids, gov_aliases = self._resolve_side(round_payload, "gov", import_key)
        opp_ids, opp_aliases = self._resolve_side(round_payload, "opp", import_key)
        action["gov"] = {"debater_ids": gov_ids}
        action["opp"] = {"debater_ids": opp_ids}
        action["imported_metadata"] = {
            "gov_1": gov_aliases[0],
            "gov_2": gov_aliases[1],
            "opp_1": opp_aliases[0],
            "opp_2": opp_aliases[1],
            "raw_result_code": str(round_payload.get("victor") or ""),
            "raw_outcome_text": str(round_payload.get("label") or ""),
            "source_import_ids": [self.import_row.id],
            "judges": self._resolve_judges(round_payload.get("judges"), import_key),
        }
        return action

    def _resolve_side(self, round_payload, side_key, import_key):
        side_payload = round_payload.get(side_key)
        if not isinstance(side_payload, dict):
            raise MittabBundleImportError(f"Round '{import_key}' is missing side '{side_key}'.")

        debater_refs = side_payload.get("debater_ids")
        source_names = side_payload.get("source_names")
        if not isinstance(debater_refs, list) or len(debater_refs) != 2:
            raise MittabBundleImportError(
                f"Round '{import_key}' side '{side_key}' must contain exactly two debater ids."
            )
        if not isinstance(source_names, list) or len(source_names) != 2:
            raise MittabBundleImportError(
                f"Round '{import_key}' side '{side_key}' must contain exactly two source names."
            )

        resolved_ids = []
        aliases = []
        for bundle_debater_id, source_name in zip(debater_refs, source_names):
            debater = self._resolve_competitor(bundle_debater_id)
            resolved_ids.append(debater.id)
            aliases.append(
                {
                    "debater_id": debater.id,
                    "source_name": str(source_name or "").strip() or debater.name,
                }
            )
        return resolved_ids, aliases

    def _resolve_competitor(self, bundle_debater_id):
        debater_payload = self.debaters_by_id.get(int(bundle_debater_id))
        if debater_payload is None:
            raise MittabBundleImportError(f"Unknown bundle debater id: {bundle_debater_id}.")

        apda_id = debater_payload.get("apda_id")
        if apda_id not in (None, ""):
            existing = Debater.all_objects.filter(pk=int(apda_id)).first()
            if existing is not None:
                return existing

        name = str(debater_payload.get("name") or "").strip()
        exact_matches = self._exact_name_matches(name)
        if len(exact_matches) == 1:
            return exact_matches[0]
        if len(exact_matches) > 1:
            disambiguated = self._select_competitor_match(exact_matches)
            if disambiguated is not None:
                return disambiguated

        school = self._resolve_debater_school(debater_payload)
        first_name, last_name = _split_name(name)
        return Debater.all_objects.create(
            first_name=first_name,
            last_name=last_name,
            school=school,
            synthetic=True,
            temporary=False,
        )

    def _resolve_debater_school(self, debater_payload):
        school_id = debater_payload.get("school_id")
        if school_id in (None, ""):
            return None
        school_payload = self.schools_by_id.get(int(school_id))
        if school_payload is None:
            raise MittabBundleImportError(f"Unknown bundle school id: {school_id}.")
        return self._resolve_school_payload(school_payload)

    def _resolve_school_payload(self, school_payload):
        apda_id = school_payload.get("apda_id")
        if apda_id not in (None, ""):
            existing = School.all_objects.filter(pk=int(apda_id)).first()
            if existing is not None:
                return existing

        name = str(school_payload.get("name") or "").strip()
        if not name:
            return None
        existing = School.all_objects.filter(name__iexact=name).first()
        if existing is not None:
            return existing
        return School.all_objects.create(name=name, short_name=name[:64], temporary=True)

    def _select_competitor_match(self, exact_matches):
        tournament_matches = list(
            Debater.all_objects.filter(
                pk__in=[debater.id for debater in exact_matches],
                teams__team_results__tournament=self.tournament,
            )
            .distinct()
            .order_by("id")
        )
        if tournament_matches:
            exact_matches = tournament_matches

        non_synthetic_matches = [debater for debater in exact_matches if not debater.synthetic]
        if non_synthetic_matches:
            return non_synthetic_matches[0]

        return exact_matches[0] if exact_matches else None

    def _resolve_judges(self, judge_payloads, import_key):
        if judge_payloads in (None, ""):
            return []
        if not isinstance(judge_payloads, list):
            raise MittabBundleImportError(f"Round '{import_key}' judges must be a list.")

        resolved = []
        chair_seen = 0
        for judge_payload in judge_payloads:
            if not isinstance(judge_payload, dict):
                raise MittabBundleImportError(f"Round '{import_key}' judge entries must be objects.")
            original_name = str(judge_payload.get("original_name") or "").strip()
            if not original_name:
                raise MittabBundleImportError(f"Round '{import_key}' includes a judge without a name.")
            is_chair = bool(judge_payload.get("is_chair"))
            if is_chair:
                chair_seen += 1
            debater = self._resolve_judge_identity(original_name)
            resolved.append(
                {
                    "original_name": original_name,
                    "is_chair": is_chair,
                    "debater_id": debater.id,
                    "source_name": original_name,
                }
            )

        if chair_seen > 1:
            raise MittabBundleImportError(f"Round '{import_key}' includes more than one chair judge.")
        return resolved

    def _resolve_judge_identity(self, name):
        exact_matches = self._exact_name_matches(name)
        if len(exact_matches) == 1:
            return exact_matches[0]

        alias_matches = list(
            DebaterAlias.objects.filter(
                source_name__iexact=name,
                debater__synthetic=True,
            )
            .select_related("debater")
            .order_by("id")
        )
        unique_alias_debaters = []
        seen_ids = set()
        for alias in alias_matches:
            if alias.debater_id in seen_ids:
                continue
            seen_ids.add(alias.debater_id)
            unique_alias_debaters.append(alias.debater)
        if len(unique_alias_debaters) == 1:
            return unique_alias_debaters[0]

        first_name, last_name = _split_name(name)
        return Debater.all_objects.create(
            first_name=first_name,
            last_name=last_name,
            synthetic=True,
            temporary=False,
        )

    def _exact_name_matches(self, full_name):
        first_name, last_name = _split_name(full_name)
        return list(
            Debater.all_objects.filter(
                first_name__iexact=first_name,
                last_name__iexact=last_name,
            ).order_by("id")
        )

    def _index_rows(self, key):
        indexed = {}
        for row in self.document.get(key, []):
            if not isinstance(row, dict):
                raise MittabBundleImportError(f"Mit-Tab bundle '{key}' rows must be objects.")
            row_id = row.get("id")
            if row_id in (None, ""):
                raise MittabBundleImportError(f"Each Mit-Tab bundle '{key}' row requires an id.")
            normalized_id = int(row_id)
            if normalized_id in indexed:
                raise MittabBundleImportError(f"Duplicate id '{normalized_id}' found in '{key}'.")
            indexed[normalized_id] = row
        return indexed


def _split_name(full_name):
    cleaned = str(full_name or "").strip()
    if not cleaned:
        raise MittabBundleImportError("Encountered a blank participant name in the Mit-Tab bundle.")
    parts = cleaned.split()
    first_name = parts[0]
    last_name = " ".join(parts[1:]) if len(parts) > 1 else ""
    return first_name, last_name
