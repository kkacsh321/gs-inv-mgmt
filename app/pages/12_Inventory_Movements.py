from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.page_common import repo_context, setup_page
from app.components.views.inventory_movements import render_inventory_movements

setup_page("Inventory Movements")

with repo_context() as repo:
    render_inventory_movements(repo)
