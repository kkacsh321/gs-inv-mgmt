from types import SimpleNamespace
from unittest.mock import patch

from app.services.lifecycle_retention import cleanup_lifecycle_retention


def test_cleanup_lifecycle_retention_uses_runtime_retain_days() -> None:
    class Repo:
        def __init__(self) -> None:
            self.called = {}

        def cleanup_archived_media_assets(self, *, retain_days: int, actor: str):
            self.called["media"] = {"retain_days": int(retain_days), "actor": str(actor)}
            return {"deleted_archived_media": 3}

        def cleanup_archived_listings(self, *, retain_days: int, actor: str):
            self.called["listing"] = {"retain_days": int(retain_days), "actor": str(actor)}
            return {"deleted_archived_listings": 2, "skipped_listings_with_dependencies": 1}

        def cleanup_archived_purchase_lots(self, *, retain_days: int, actor: str):
            self.called["lot"] = {"retain_days": int(retain_days), "actor": str(actor)}
            return {"deleted_archived_lots": 1, "skipped_lots_with_dependencies": 0}

        def cleanup_archived_products(self, *, retain_days: int, actor: str):
            self.called["product"] = {"retain_days": int(retain_days), "actor": str(actor)}
            return {"deleted_archived_products": 4, "skipped_products_with_dependencies": 2}

    repo = Repo()
    runtime_values = {
        "lifecycle_media_archive_retain_days": 120,
        "lifecycle_listing_archive_retain_days": 365,
        "lifecycle_lot_archive_retain_days": 400,
        "lifecycle_product_archive_retain_days": 500,
    }
    with patch(
        "app.services.lifecycle_retention.get_runtime_int",
        side_effect=lambda _repo, key, default: int(runtime_values.get(key, default)),
    ):
        result = cleanup_lifecycle_retention(repo, actor="runner")

    assert repo.called["media"] == {"retain_days": 120, "actor": "runner"}
    assert repo.called["listing"] == {"retain_days": 365, "actor": "runner"}
    assert repo.called["lot"] == {"retain_days": 400, "actor": "runner"}
    assert repo.called["product"] == {"retain_days": 500, "actor": "runner"}
    assert result["retain_days_media"] == 120
    assert result["deleted_archived_media"] == 3
    assert result["deleted_archived_listings"] == 2
    assert result["deleted_archived_lots"] == 1
    assert result["deleted_archived_products"] == 4
    assert result["skipped_listings_with_dependencies"] == 1
    assert result["skipped_products_with_dependencies"] == 2
