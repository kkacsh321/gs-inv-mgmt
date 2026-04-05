from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.components.views.sync import render_sync
from app.page_common import repo_context, setup_page

setup_page("Sync")

with repo_context() as repo:
    render_sync(repo)
