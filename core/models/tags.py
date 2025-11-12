from django.db import models
from taggit.models import TagBase, GenericTaggedItemBase


class ResourceTag(TagBase):
    """Custom tag model for resources."""
    class Meta:
        verbose_name = "Resource Tag"
        verbose_name_plural = "Resource Tags"


class TaggedResource(GenericTaggedItemBase):
    """Through model for resource tags."""
    tag = models.ForeignKey(
        ResourceTag,
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_items",
    )
