#!/usr/bin/env python3

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main():
    parser = argparse.ArgumentParser(
        description="Generate round amendments that correct inround speaker positions from raw backup files."
    )
    parser.add_argument("--source-root", required=True, help="Root directory containing raw backup folders.")
    parser.add_argument("--output", required=True, help="Path to the amendment JSON output file.")
    parser.add_argument("--report", help="Optional path to a report JSON file.")
    parser.add_argument("--debater-id", type=int, help="Filter rounds to a specific debater id.")
    parser.add_argument("--debater-name", help="Filter rounds to a specific debater name.")
    parser.add_argument("--round-id", dest="round_ids", action="append", type=int, help="Filter to one or more specific round ids.")
    args = parser.parse_args()

    if not args.debater_id and not args.debater_name and not args.round_ids:
        parser.error("Pass at least one of --debater-id, --debater-name, or --round-id.")

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "apda.settings.base")

    import django

    django.setup()

    from core.utils.speaker_position_amendments import (
        generate_speaker_position_amendment_document,
        write_amendment_document,
        write_report_document,
    )

    document, report = generate_speaker_position_amendment_document(
        source_root=args.source_root,
        debater_id=args.debater_id,
        debater_name=args.debater_name,
        round_ids=args.round_ids,
    )
    write_amendment_document(document=document, output_path=args.output)
    if args.report:
        write_report_document(report=report, output_path=args.report)

    print(
        f"target_rounds={report['summary']['target_rounds']} "
        f"actions_written={report['summary']['actions_written']} "
        f"already_correct={report['summary']['already_correct']} "
        f"missing_backup_file={report['summary']['missing_backup_file']} "
        f"no_unique_backup_match={report['summary']['no_unique_backup_match']} "
        f"unmatched_backup_names={report['summary']['unmatched_backup_names']}"
    )
    print(f"amendments={Path(args.output)}")
    if args.report:
        print(f"report={Path(args.report)}")


if __name__ == "__main__":
    sys.exit(main())
