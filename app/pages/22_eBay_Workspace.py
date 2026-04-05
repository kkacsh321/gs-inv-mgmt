from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.page_common import repo_context, setup_page
from app.components.views.ebay_workspace import render_ebay_workspace

setup_page("eBay Workspace")

with repo_context() as repo:
    render_ebay_workspace(repo)

