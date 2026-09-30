"""Service and operator diagnostics must resolve the same default paths."""
from pathlib import Path
import os


def data_directory(value=None):
    return Path(value or os.getenv('FACTORY_CONTROL_DATA', '~/.factory/control')).expanduser().resolve()


def workspace_directory(value=None):
    return Path(value or os.getenv('FACTORY_WORKSPACE_ROOT', '~/projects')).expanduser().resolve()


def static_directory(value=None):
    return Path(value or os.getenv('FACTORY_STATIC_DIR') or
                Path(__file__).resolve().parents[2] / 'frontend' / 'dist').expanduser().resolve()
