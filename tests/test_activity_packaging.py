from pathlib import Path
import tempfile
import unittest

from build_tools.activity_resources import stage_activities


class ActivityPackagingTests(unittest.TestCase):
    def test_only_registered_modules_and_shared_runtime_are_packaged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            activities = root/'activities'
            activities.mkdir()
            (activities/'__init__.py').write_text('', 'utf-8')
            for name in ('registered','checkin_common','unfinished'):
                module = activities/name
                module.mkdir()
                (module/'client.py').write_text('# public module', 'utf-8')
            (activities/'registered/manifest.json').write_text('{}', 'utf-8')
            tests = activities/'registered/tests'
            tests.mkdir()
            (tests/'test_client.py').write_text('# test only', 'utf-8')
            stage_activities(root, root/'staged')
            self.assertTrue((root/'staged/registered/client.py').is_file())
            self.assertTrue((root/'staged/checkin_common/client.py').is_file())
            self.assertFalse((root/'staged/unfinished').exists())
            self.assertFalse((root/'staged/registered/tests').exists())


if __name__ == '__main__':
    unittest.main()
