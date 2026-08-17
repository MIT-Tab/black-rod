from django.db import models
from taggit.models import GenericTaggedItemBase, TagBase, TaggedItemBase


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


class MotionTopic(TagBase):
    """A motion-only topic namespace, separate from video and resource tags."""

    class Meta:
        verbose_name = "Motion Topic"
        verbose_name_plural = "Motion Topics"


class TaggedMotion(TaggedItemBase):
    tag = models.ForeignKey(
        MotionTopic,
        on_delete=models.CASCADE,
        related_name="tagged_motion_items",
    )
    content_object = models.ForeignKey(
        "Motion",
        on_delete=models.CASCADE,
        related_name="tagged_motion_items",
    )
