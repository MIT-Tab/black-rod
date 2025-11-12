from django.conf import settings
from django.db import models
from django.shortcuts import reverse
from taggit.managers import TaggableManager

from core.models.debater import Debater


class Resource(models.Model):
    """Educational resources like cases, lectures, and write-ups."""
    
    title = models.CharField(max_length=255)
    
    # Authors - at least one required
    authors = models.ManyToManyField(
        Debater,
        related_name="resources",
        help_text="At least one author is required"
    )
    
    # Type of resource
    CASE = 'case'
    LECTURE = 'lecture'
    WRITEUP = 'writeup'
    SLIDEDECK = "slidedeck"
    OTHER = 'other'
    
    TYPE_CHOICES = [
        (CASE, 'Case'),
        (LECTURE, 'Lecture'),
        (SLIDEDECK, 'Slide Deck'),
        (WRITEUP, 'Write-up'),
        (OTHER, 'Other'),
    ]
    
    resource_type = models.CharField(
        max_length=20,
        choices=TYPE_CHOICES,
        default=OTHER,
        verbose_name="Type"
    )
    
    # Usage permissions - text blob
    usage_permissions = models.TextField(
        blank=True,
        help_text="Specify any usage permissions or restrictions for this resource"
    )
    
    # Content link
    content_link = models.URLField(
        max_length=4096,
        verbose_name="Content Link",
        help_text="Link to the resource content"
    )
    
    # Description
    description = models.TextField(blank=True)
    
    # Viewing permissions
    PUBLIC = 0
    REQUIRES_LOGIN = 1
    
    PERMISSION_CHOICES = [
        (PUBLIC, 'Public (Discoverable by Google search)'),
        (REQUIRES_LOGIN, 'Requires Login'),
    ]
    
    viewing_permission = models.IntegerField(
        choices=PERMISSION_CHOICES,
        default=PUBLIC,
        verbose_name="Viewing Permission"
    )
    
    # Tags
    tags = TaggableManager(through='core.TaggedResource', blank=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Creator (the user who created the resource)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="created_resources",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ['-created_at']

    def get_absolute_url(self):
        return reverse("core:resource_detail", kwargs={"pk": self.id})

    def __str__(self):
        return f"{self.title} ({self.get_resource_type_display()})"
    
    def is_author(self, user):
        """Check if a user is an author of this resource."""
        if not user.is_authenticated:
            return False
        # Check if any of the user's claimed debaters are authors
        return self.authors.filter(user=user).exists()
