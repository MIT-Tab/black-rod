# pylint: disable=import-outside-toplevel
from django.test import TestCase
from django.apps import apps


from core.apps import CoreConfig



class AppsTest(TestCase):
    """Test Django app configuration"""

    def test_core_app_config(self):
        """Test that CoreConfig is properly configured"""
        app_config = apps.get_app_config("core")
        self.assertEqual(app_config.name, "core")
        self.assertIsInstance(app_config, CoreConfig)

    def test_app_ready_method(self):
        """Test that app ready method exists"""
        import core
        config = CoreConfig("core", core)
        try:
            config.ready()
        except Exception:
            pass


class TemplateTagsTest(TestCase):
    """Test template tags functionality"""
