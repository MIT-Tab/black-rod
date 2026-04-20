import csv
from pathlib import Path

from django.core.management.base import BaseCommand

from core.models import GeneratedCode


class Command(BaseCommand):
    help = "Export all generated codes to a CSV file."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            default="generated_codes.csv",
            help="Path to the CSV file to write.",
        )

    def handle(self, *args, **options):
        output_path = Path(options["output"])
        output_path.parent.mkdir(parents=True, exist_ok=True)

        rows = (
            GeneratedCode.objects.select_related("user")
            .order_by("code")
            .values_list("code", "user__username", "user__email")
        )

        with output_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(["code", "username", "email"])
            writer.writerows(rows)

        self.stdout.write(
            self.style.SUCCESS(
                f"Exported {GeneratedCode.objects.count()} generated codes to {output_path}"
            )
        )
