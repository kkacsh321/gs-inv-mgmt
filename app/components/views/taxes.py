from app.repository import InventoryRepository
from app.components.views.reports import render_reports


def render_taxes(repo: InventoryRepository) -> None:
    render_reports(repo, tax_workspace=True)
