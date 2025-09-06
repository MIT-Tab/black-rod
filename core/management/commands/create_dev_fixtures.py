import random
from django.core.management.base import BaseCommand
from django.core import serializers
from django.contrib.auth.models import Permission, Group
from django.contrib.contenttypes.models import ContentType
from taggit.models import Tag, TaggedItem
from core.models import (
    User, Video, Debater, School, Team, Tournament, Round, RoundStats,
    SpeakerResult, TeamResult, COTY, NOTY, QUAL, SOTY, TOTY, TOTYReaff,
    OnlineQUAL, SiteSetting, QualPoints, Reaff, SchoolLookup
)

FAKE_PW = "pbkdf2_sha256$260000$fake$fakehashfordev"

class Command(BaseCommand):
    help = "Preconfigured data dump to create development fixtures with private data sanitized."

    def add_arguments(self, parser):
        parser.add_argument('--output', type=str, default='dev_fixtures.json')

    def handle(self, *args, **options):
        random.seed(1000)
        output_file = options['output']

        public_models = [
            School, SchoolLookup, Debater, Team, Tournament, Round, RoundStats,
            SpeakerResult, TeamResult, COTY, NOTY, QUAL, SOTY, TOTY, TOTYReaff,
            OnlineQUAL, SiteSetting, QualPoints, Reaff
        ]

        framework_models = [ContentType, Permission, Group, Tag, TaggedItem]

        all_objects = []

        for model in public_models:
            try:
                qs = model.objects.all()
                self.stdout.write(f"Processing {model.__name__}: {qs.count()} objects")
                for obj in qs.iterator(chunk_size=2000):
                    all_objects.append(obj)
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"Error processing {model.__name__}: {e}"))

        for model in framework_models:
            try:
                qs = model.objects.all()
                self.stdout.write(f"Processing {model.__name__}: {qs.count()} objects")
                for obj in qs.iterator(chunk_size=2000):
                    all_objects.append(obj)
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"Error processing {model.__name__}: {e}"))

        self._add_simulated_users(all_objects)
        self._add_simulated_videos(all_objects)

        try:
            data = serializers.serialize(
                'json',
                all_objects,
                use_natural_foreign_keys=True,
                use_natural_primary_keys=True,
                indent=None,
            )
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error serializing data: {e}"))
            return

        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(data)
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error writing to file {output_file}: {e}"))
            return

        self.stdout.write(self.style.SUCCESS(f"Successfully created {output_file} with {len(all_objects)} objects"))

    def _add_simulated_users(self, all_objects):
        users = User.objects.all()
        total = users.count()
        self.stdout.write(f"Simulating {total} users")
        first_names = [
            "Alex","Jordan","Taylor","Casey","Riley","Quinn","Avery","Cameron",
            "Drew","Sage","River","Phoenix","Rowan","Skylar","Emery","Finley"
        ]
        for idx, user in enumerate(users.iterator(chunk_size=2000)):
            simulated_user = User(
                id=user.id,
                username=f"user_{user.id}",
                first_name=first_names[idx % len(first_names)],
                last_name=first_names[(total - idx - 1) % len(first_names)],
                email=f"user_{user.id}@example.com",
                is_staff=user.is_staff,
                is_active=user.is_active,
                is_superuser=user.is_superuser,
                date_joined=user.date_joined,
                last_login=user.last_login,
                can_view_private_videos=getattr(user, "can_view_private_videos", False),
                password=FAKE_PW,
            )
            simulated_user._state.adding = False
            all_objects.append(simulated_user)

    def _add_simulated_videos(self, all_objects):
        videos = Video.objects.all()
        total = videos.count()
        self.stdout.write(f"Simulating {total} videos")

        fake_youtube_ids = [
            "dQw4w9WgXcQ","jNQXAC9IVRw","9bZkp7q19f0","fJ9rUzIMcZQ","kJQP7kiw5Fk",
            "YQHsXMglC9A","pRpeEdMmmQ0","OPf0YbXqDm0","CevxZvSJLk8","hTWKbfoikeg",
        ]
        fake_cases = [
            "<p>This house would implement universal basic income.</p>",
            "<p>This house believes that artificial intelligence poses a greater threat than benefit to humanity.</p>",
            "<p>This house would ban private schools.</p>",
            "<p>This house supports the use of nuclear energy as a primary source of power.</p>",
            "<p>This house would legalize all drugs.</p>",
            "<p>This house believes that democratic governments should prioritize economic growth over environmental protection.</p>",
            "<p>This house would abolish the death penalty worldwide.</p>",
            "<p>This house supports mandatory military service.</p>",
            "<p>This house would implement a four-day work week.</p>",
            "<p>This house believes that social media companies should be regulated as public utilities.</p>",
            "<p>This house would ban genetic modification of human embryos.</p>",
            "<p>This house supports the decriminalization of prostitution.</p>",
            "<p>This house would implement a wealth tax on billionaires.</p>",
            "<p>This house believes that voting should be mandatory.</p>",
            "<p>This house would ban the use of animals in scientific research.</p>",
        ]
        fake_descriptions = [
            "<p>A comprehensive analysis of the motion with strong arguments on both sides.</p>",
            "<p>Excellent clash between teams with well-developed cases.</p>",
            "<p>Strategic debate with innovative approaches to the topic.</p>",
            "<p>High-level debate featuring experienced debaters.</p>",
            "<p>Educational round demonstrating various debate techniques.</p>",
            "<p>Competitive round with strong research and preparation evident.</p>",
            "<p>Well-argued positions with effective rebuttals.</p>",
            "<p>Demonstration of advanced debate strategy and tactics.</p>",
            "<p>Engaging debate with clear structure and reasoning.</p>",
            "<p>Example of effective parliamentary debate style.</p>",
        ]

        for idx, v in enumerate(videos.iterator(chunk_size=2000)):
            sv = Video(
                id=v.id,
                pm_id=v.pm_id,
                lo_id=v.lo_id,
                mg_id=v.mg_id,
                mo_id=v.mo_id,
                tournament_id=v.tournament_id,
                round=v.round,
                case=fake_cases[idx % len(fake_cases)],
                description=fake_descriptions[idx % len(fake_descriptions)],
                link=f"https://www.youtube.com/watch?v={fake_youtube_ids[idx % len(fake_youtube_ids)]}",
                password="",
                permissions=v.permissions,
            )
            sv._state.adding = False
            all_objects.append(sv)
