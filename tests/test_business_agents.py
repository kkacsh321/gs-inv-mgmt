from app.services.business_agents import (
    build_business_chat_room_plan,
    build_business_chat_room_roster,
    business_agent_domain_scopes,
    business_agent_labels,
    detect_business_agent_mentions,
    get_business_agent,
    resolve_business_agent_key,
)


def test_business_agent_registry_includes_named_specialists():
    kurt = get_business_agent("kurt_intake_agent")
    murdock = get_business_agent("murdock_listing_agent")

    assert kurt is not None
    assert kurt.name == "Kurt"
    assert kurt.write_capable is True
    assert "inventory" in kurt.domains
    assert "kurt" in kurt.slack_aliases

    assert murdock is not None
    assert murdock.name == "Murdock"
    assert murdock.write_capable is True
    assert "listings" in murdock.domains
    assert "murdock" in murdock.slack_aliases


def test_business_agent_helpers_expose_labels_and_domain_scopes():
    labels = business_agent_labels()
    scopes = business_agent_domain_scopes()

    assert labels["kurt_intake_agent"] == "Kurt (Inventory Intake)"
    assert labels["murdock_listing_agent"] == "Murdock (Listing + Sales Copy)"
    assert "inventory" in scopes["kurt_intake_agent"]
    assert "listings" in scopes["murdock_listing_agent"]
    assert "customers" in scopes["goldie_accountant_agent"]
    assert "customers" in scopes["business_monitor_agent"]


def test_business_chat_room_plan_lists_specialists_and_approval_rules():
    plan = build_business_chat_room_plan(
        prompt="create an eBay listing for product 123",
        selected_agent="murdock_listing_agent",
        allowed_domains={"inventory", "listings", "sales", "accounting"},
    )

    assert plan["room"] == "GoldenStackers Business Chat Room"
    assert plan["primary_agent"] == "Murdock (Listing + Sales Copy)"
    assert "listings" in plan["effective_domains"]
    assert "accounting" not in plan["effective_domains"]
    assert any("writes require approval" in rule for rule in plan["coordination_rules"])

    atlas_plan = build_business_chat_room_plan(
        prompt="which repeat buyers need follow up",
        selected_agent="business_monitor_agent",
        allowed_domains={"customers", "orders", "reports"},
    )
    assert "customers" in atlas_plan["effective_domains"]

    roster = build_business_chat_room_roster()
    names = {row["name"] for row in roster}
    assert {"Kurt", "Murdock", "Goldie"}.issubset(names)


def test_business_agent_alias_resolution_and_mentions():
    assert resolve_business_agent_key("Kurt") == "kurt_intake_agent"
    assert resolve_business_agent_key("inventory-intake") == "kurt_intake_agent"
    assert resolve_business_agent_key("Murdock") == "murdock_listing_agent"
    assert resolve_business_agent_key("listing") == "murdock_listing_agent"
    assert resolve_business_agent_key("Goldie") == "goldie_accountant_agent"

    mentions = detect_business_agent_mentions(
        "Kurt please intake this lot, then @Murdock draft the listing and Goldie review cost basis."
    )

    assert mentions == ["kurt_intake_agent", "murdock_listing_agent", "goldie_accountant_agent"]
