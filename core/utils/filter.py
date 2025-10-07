from collections.abc import Iterable

import django_filters
from django.core.exceptions import FieldDoesNotExist
from taggit.forms import TagField
from taggit.managers import TaggableManager


class TagFilter(django_filters.CharFilter):
    field_class = TagField

    def __init__(self, *args, match="any", **kwargs):
        self.match = match
        kwargs.setdefault("field_name", "tagged_items__tag__name")
        super().__init__(*args, **kwargs)

    def _normalize(self, value):
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, Iterable):
            values = list(value)
        else:
            values = [value]

        normalized = []
        for item in values:
            if not item:
                continue
            normalized.append(getattr(item, "name", str(item)))

        seen = set()
        deduped = []
        for item in normalized:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)

        return deduped

    def _resolve_path(self, qs):
        if "__" in self.field_name:
            return self.field_name

        try:
            field = qs.model._meta.get_field(self.field_name)
        except FieldDoesNotExist:
            return self.field_name

        if isinstance(field, TaggableManager):
            if getattr(field, "use_gfk", False):
                return "tagged_items__tag__name"
            return f"{self.field_name}__name"

        return self.field_name

    def filter(self, qs, value):
        if not value:
            return qs

        normalized = self._normalize(value)
        if not normalized:
            return qs

        path = self._resolve_path(qs)

        if self.match == "all":
            for tag_name in normalized:
                qs = qs.filter(**{path: tag_name})
            return qs.distinct()

        return qs.filter(**{f"{path}__in": normalized}).distinct()
