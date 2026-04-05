from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.page_common import build_storage, repo_context, setup_page
from app.components.views.tools import render_tools
from app.services.spot_price import SpotPriceService

setup_page("Tools")

with repo_context() as repo:
    spot = SpotPriceService(repo)
    storage = build_storage()
    render_tools(spot, repo, storage)
