"""Describe this Python process truthfully, without exporting private argv/env."""
from pathlib import Path
import os
import platform
import sys
import time
import traceback

STARTED = time.monotonic()
INITIALIZED = int(time.time()*1000)


def collect():
    machine = platform.machine().lower()
    architecture = {'amd64': 'x64', 'x86_64': 'x64', 'aarch64': 'arm64'}.get(machine, machine)
    return {
        'runtime_type': 'Unknown non-browser environment', 'python_version': platform.python_version(),
        'platform': sys.platform, 'arch': architecture,
        'pid': float(os.getpid()), 'ppid': float(os.getppid()),
        'exec_path': Path(sys.executable).name,
        'argv': [Path(sys.argv[0]).name] if sys.argv else [],
        'environment_variables': {key: os.environ[key] for key in ('SystemRoot', 'WINDIR') if key in os.environ},
        'debugger_detected': int(sys.gettrace() is not None),
        'init_time': INITIALIZED,
        'time_elapsed': int((time.monotonic()-STARTED)*1000),
        'uptime_seconds': round(time.monotonic()-STARTED, 6),
        'last_call_stack': [Path(frame.filename).name+':'+str(frame.lineno)+':'+frame.name
                            for frame in traceback.extract_stack(limit=8)],
    }
