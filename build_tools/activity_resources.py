"""Stage registered activities and their shared runtime, excluding unfinished work."""
from pathlib import Path
import shutil


def stage_activities(root, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for source in (Path(root)/'activities').iterdir():
        if source.is_file() and source.suffix == '.py':
            shutil.copy2(source, destination/source.name)
        elif source.is_dir() and (source.name == 'checkin_common' or (source/'manifest.json').is_file()):
            shutil.copytree(source, destination/source.name,
                ignore=shutil.ignore_patterns('__pycache__', 'tests', 'android'))
