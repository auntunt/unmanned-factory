"""Keep coordinator deployment keys outside all worker-visible filesystems."""
import os
from pathlib import Path


def hidden_deploy_dir(workspace=None):
    raw = os.getenv('FACTORY_DEPLOY_KEY_DIR')
    if not raw:
        return None
    path = Path(raw).resolve()
    if not Path(raw).is_absolute() or path == Path('/'):
        raise ValueError('Deployment key directory must be an absolute dedicated directory')
    if workspace is not None:
        workspace = Path(workspace).resolve()
        if path.is_relative_to(workspace) or workspace.is_relative_to(path):
            raise ValueError('Project workspace overlaps coordinator credentials')
    return path if path.exists() else None
