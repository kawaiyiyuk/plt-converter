import os
import tempfile
import unittest
from unittest.mock import patch

class FactoryIsolationTests(unittest.TestCase):
    def test_legacy_factory_does_not_register_projection(self):
        from app import create_app
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'PLT_TEMP_FOLDER': root}):
            app = create_app()
        self.assertFalse(any('/projection/' in str(rule) for rule in app.url_map.iter_rules()))
