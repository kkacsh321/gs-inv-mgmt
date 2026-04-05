from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.page_common import repo_context, setup_page
from app.components.views.operations_home import render_operations_home

setup_page("Operations Home")

with repo_context() as repo:
    render_operations_home(repo)
