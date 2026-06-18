import json
from datetime import timedelta
from decimal import Decimal

import pytest

from app.db.models import Customer
from app.services.business_chat_room import (
    BUSINESS_CHAT_ENTITY_TYPE,
    DEFAULT_BUSINESS_ROOM_KEY,
    build_business_room_handoff_review_card,
    build_business_room_action_draft_payload,
    build_business_room_action_draft_contract,
    build_business_room_agent_answer_evidence,
    build_business_room_answer_command_suggestions,
    build_business_room_agent_activity_summary,
    build_business_room_agent_focus_summary,
    build_business_room_agent_prompt_board,
    build_business_room_agent_workload_summary,
    build_business_room_coordination_suggestions,
    build_business_room_standup_brief,
    build_business_room_operator_answer_rows,
    build_business_room_context_snapshot,
    business_room_workflow_page_path,
    build_business_room_message_payload,
    classify_business_room_action_route,
    infer_business_room_attachment_kind,
    infer_business_room_reply_targets,
    is_business_room_write_intent,
    apply_business_room_agent_answer_to_latest_handoff,
    list_business_room_action_requests,
    list_business_room_active_workflow_handoffs,
    list_business_room_messages,
    mark_business_room_workflow_handoff_reviewed,
    list_business_room_workflow_handoffs,
    normalize_business_room_key,
    plan_business_room_followup_agents,
    plan_business_room_agent_responses,
    queue_business_room_action_request,
    record_business_room_message,
    record_business_room_turn,
    save_business_room_action_workflow_draft,
    transition_business_room_action_request,
)
from app.utils.time import utcnow_naive
from test_support import in_memory_repo


def test_business_room_message_payload_normalizes_agent_label_and_room_key():
    payload = build_business_room_message_payload(
        room_key=" GoldenStackers Business ",
        sender_type="agent",
        sender_key="kurt_intake_agent",
        message="Need cost basis for this lot.",
        directed_to=["Murdock_Listing_Agent"],
    )

    assert payload["room_key"] == "goldenstackers_business"
    assert payload["sender_label"] == "Kurt (Inventory Intake)"
    assert payload["directed_to"] == ["murdock_listing_agent"]
    assert payload["message"] == "Need cost basis for this lot."


def test_business_room_message_payload_infers_directed_agents_from_message():
    payload = build_business_room_message_payload(
        sender_type="user",
        sender_key="keith",
        message="Kurt intake this purchase, Murdock draft the listing, and Goldie review cost basis.",
        source="app",
    )

    assert payload["directed_to"] == [
        "kurt_intake_agent",
        "murdock_listing_agent",
        "goldie_accountant_agent",
    ]


def test_business_room_message_payload_normalizes_explicit_alias_targets():
    payload = build_business_room_message_payload(
        sender_type="user",
        sender_key="keith",
        message="Please coordinate this.",
        directed_to=["inventory-intake", "listing", "Goldie", "listing"],
        source="app",
    )

    assert payload["directed_to"] == [
        "kurt_intake_agent",
        "murdock_listing_agent",
        "goldie_accountant_agent",
    ]


def test_business_room_agent_response_plan_routes_by_keywords_and_mentions():
    assert plan_business_room_agent_responses(message="Kurt intake this purchase", max_agents=3) == [
        "kurt_intake_agent"
    ]
    assert plan_business_room_agent_responses(message="Need comps and an ebay listing draft", max_agents=3) == [
        "murdock_listing_agent",
        "research_pricing_agent",
    ]
    assert plan_business_room_agent_responses(message="Everyone review this business priority", max_agents=5) == [
        "business_monitor_agent",
        "kurt_intake_agent",
        "murdock_listing_agent",
        "research_pricing_agent",
        "goldie_accountant_agent",
    ]
    assert plan_business_room_agent_responses(
        message="Please coordinate this.",
        directed_to=["Goldie", "listing"],
        max_agents=3,
    ) == ["goldie_accountant_agent", "murdock_listing_agent"]


def test_business_room_reply_targets_include_human_and_agent_handoffs():
    targets = infer_business_room_reply_targets(
        reply_text="I can draft the eBay copy after Scout checks comps. Goldie should review cost basis.",
        human_key="Keith",
        sender_agent_key="murdock_listing_agent",
    )

    assert targets == ["keith", "research_pricing_agent", "goldie_accountant_agent"]


def test_business_room_followup_agents_are_bounded_and_skip_responded_agents():
    followups = plan_business_room_followup_agents(
        reply_text="Scout should comp this, then Goldie should review cost basis and Murdock can draft copy.",
        sender_agent_key="kurt_intake_agent",
        already_responded=["kurt_intake_agent", "research_pricing_agent"],
        max_agents=4,
    )

    assert followups == ["murdock_listing_agent", "goldie_accountant_agent"]

    no_slots = plan_business_room_followup_agents(
        reply_text="Scout should comp this.",
        sender_agent_key="murdock_listing_agent",
        already_responded=[
            "murdock_listing_agent",
            "kurt_intake_agent",
            "goldie_accountant_agent",
        ],
        max_agents=3,
    )

    assert no_slots == []


def test_business_room_attachment_kind_inference():
    assert infer_business_room_attachment_kind("image/jpeg", "photo.jpg") == "image"
    assert infer_business_room_attachment_kind("video/quicktime", "clip.mov") == "video"
    assert infer_business_room_attachment_kind("application/octet-stream", "clip.mov") == "video"
    assert infer_business_room_attachment_kind("application/pdf", "invoice.pdf") == "pdf"
    assert infer_business_room_attachment_kind("text/csv", "research.csv") == "text"


def test_business_room_write_intent_and_approval_queue_payload():
    class Repo:
        def __init__(self):
            self.created = []
            self.updated = []

        def create_integration_queue_job(self, **kwargs):
            self.created.append(kwargs)
            return type("Row", (), {"id": 77})()

        def update_integration_queue_job(self, job_id, updates, actor):
            self.updated.append({"job_id": job_id, "updates": updates, "actor": actor})

    assert is_business_room_write_intent("Murdock, create a listing draft for product 12")
    assert not is_business_room_write_intent("Murdock, what are the blockers for this listing?")
    assert not is_business_room_write_intent("Kurt answer quantity: 20")

    repo = Repo()
    result = queue_business_room_action_request(
        repo,
        message="Murdock, create a listing draft for product 12",
        actor="keith",
        user_role="admin",
        directed_to=["murdock_listing_agent"],
        attachments=[{"filename": "coin.jpg", "kind": "image"}],
        source_message_id=123,
        environment="prod",
    )

    assert result["queue_job_id"] == 77
    assert result["status"] == "pending_approval"
    assert result["action_route"]["route_key"] == "listing"
    assert result["action_route"]["recommended_workflow"] == "listing_wizard"
    assert result["has_draft_contract"] is True
    assert result["draft_signature"]
    queued_payload = json.loads(repo.created[0]["payload_json"])
    assert queued_payload["draft_contract"]["agent_key"] == "murdock_listing_agent"
    assert queued_payload["draft_contract"]["draft_type"] == "listing"
    assert queued_payload["apply_plan"]["reason"] == "missing_required_confirmations"
    assert repo.created[0]["integration"] == "business_chat_room"
    assert repo.created[0]["action"] == "write_action_request"
    assert repo.created[0]["max_retries"] == 0
    assert repo.updated[0]["updates"]["status"] == "blocked"


def test_business_room_agent_answer_evidence_normalizes_concise_reply():
    evidence = build_business_room_agent_answer_evidence(
        message="Murdock answer condition id: 3000",
        actor="keith",
    )

    assert evidence["type"] == "ai_agent_answer"
    assert evidence["agent"] == "murdock"
    assert evidence["agent_key"] == "murdock_listing_agent"
    assert evidence["agent_label"] == "Murdock (Listing + Sales Copy)"
    assert evidence["field"] == "condition_id"
    assert evidence["answer"] == "3000"
    assert evidence["target_queue_job_id"] == 0
    assert evidence["target_draft_id"] == 0
    assert evidence["actor"] == "keith"
    assert evidence["write_executed"] is False


def test_business_room_agent_answer_evidence_preserves_target_handoff():
    evidence = build_business_room_agent_answer_evidence(
        message="Murdock answer handoff 88 condition id: 3000",
        actor="keith",
    )

    assert evidence["agent_key"] == "murdock_listing_agent"
    assert evidence["field"] == "condition_id"
    assert evidence["target_queue_job_id"] == 88
    assert evidence["target_draft_id"] == 0


def test_apply_business_room_agent_answer_to_latest_handoff_updates_draft_contract():
    class Row:
        id = 321
        workflow_key = "inventory_intake_wizard"
        username = "keith"
        scope_key = "business_chat_room:88"
        status = "active"
        created_at = None
        updated_at = None
        draft_json = json.dumps(
            {
                "source": "business_chat_room",
                "schema": "business_room_action_handoff_v1",
                "queue_job_id": 88,
                "prompt": "Kurt intake this lot",
                "requester": {"username": "keith"},
                "action_route": {"recommended_workflow": "inventory_intake_wizard"},
                "draft_contract": {
                    "contract": {"type": "ai_agent_draft", "version": 1},
                    "signature": "before",
                    "agent_key": "kurt_intake_agent",
                    "draft_type": "intake",
                    "fields": [
                        {"key": "title", "value": "Mixed coin lot", "confidence": 0.9},
                        {"key": "category", "value": "coins", "confidence": 0.8},
                        {"key": "quantity", "value": "", "confidence": 0.0},
                    ],
                    "missing_questions": [
                        {"field": "quantity", "question": "Confirm Quantity.", "blocking": True},
                    ],
                    "proposed_actions": [],
                    "warnings": [],
                    "approval": {"required": True, "status": "pending"},
                },
                "apply_plan": {"status": "blocked", "reason": "missing_required_confirmations"},
            }
        )

    class Repo:
        def __init__(self):
            self.saved = []

        def list_workflow_drafts(self, **_kwargs):
            return [Row()]

        def save_workflow_draft(self, **kwargs):
            self.saved.append(kwargs)
            return type("Saved", (), {"id": 654})()

    repo = Repo()
    result = apply_business_room_agent_answer_to_latest_handoff(
        repo,
        environment="prod",
        username="keith",
        actor="keith",
        answer_evidence={
            "agent": "kurt",
            "agent_key": "kurt_intake_agent",
            "field": "quantity",
            "answer": "20",
            "source": "business_chat_room",
        },
    )

    assert result["applied"] is True
    assert result["workflow_key"] == "inventory_intake_wizard"
    saved_payload = repo.saved[0]["draft_payload"]
    fields = {row["key"]: row["value"] for row in saved_payload["draft_contract"]["fields"]}
    assert fields["quantity"] == "20"
    assert saved_payload["draft_contract"]["missing_questions"] == []
    assert saved_payload["apply_plan"]["reason"] == "pending_human_approval"
    assert saved_payload["operator_answers"][0]["field"] == "quantity"
    assert repo.saved[0]["last_step"] == "business_room_answer_applied"


def test_apply_business_room_agent_answer_to_target_handoff_updates_matching_queue_job():
    class Row:
        def __init__(self, row_id, queue_job_id, quantity):
            self.id = row_id
            self.workflow_key = "inventory_intake_wizard"
            self.username = "keith"
            self.scope_key = f"business_chat_room:{queue_job_id}"
            self.status = "active"
            self.created_at = None
            self.updated_at = None
            self.draft_json = json.dumps(
                {
                    "source": "business_chat_room",
                    "schema": "business_room_action_handoff_v1",
                    "queue_job_id": queue_job_id,
                    "prompt": "Kurt intake this lot",
                    "requester": {"username": "keith"},
                    "action_route": {"recommended_workflow": "inventory_intake_wizard"},
                    "draft_contract": {
                        "contract": {"type": "ai_agent_draft", "version": 1},
                        "signature": f"before-{queue_job_id}",
                        "agent_key": "kurt_intake_agent",
                        "draft_type": "intake",
                        "fields": [
                            {"key": "title", "value": f"Lot {queue_job_id}", "confidence": 0.9},
                            {"key": "category", "value": "coins", "confidence": 0.8},
                            {"key": "quantity", "value": quantity, "confidence": 0.0},
                        ],
                        "missing_questions": [
                            {"field": "quantity", "question": "Confirm Quantity.", "blocking": True},
                        ],
                        "proposed_actions": [],
                        "warnings": [],
                        "approval": {"required": True, "status": "pending"},
                    },
                }
            )

    class Repo:
        def __init__(self):
            self.saved = []

        def list_workflow_drafts(self, **_kwargs):
            return [Row(1, 88, ""), Row(2, 99, "")]

        def save_workflow_draft(self, **kwargs):
            self.saved.append(kwargs)
            return type("Saved", (), {"id": 777})()

    repo = Repo()
    result = apply_business_room_agent_answer_to_latest_handoff(
        repo,
        environment="prod",
        username="keith",
        actor="keith",
        answer_evidence={
            "agent": "kurt",
            "field": "quantity",
            "answer": "20",
            "target_queue_job_id": 99,
            "source": "business_chat_room",
        },
    )

    assert result["applied"] is True
    assert result["scope_key"] == "business_chat_room:99"
    saved_payload = repo.saved[0]["draft_payload"]
    assert saved_payload["queue_job_id"] == 99
    fields = {row["key"]: row["value"] for row in saved_payload["draft_contract"]["fields"]}
    assert fields["title"] == "Lot 99"
    assert fields["quantity"] == "20"


def test_business_room_answer_command_suggestions_use_targeted_handoff_syntax():
    handoff = {
        "id": 321,
        "queue_job_id": 88,
        "workflow_key": "inventory_intake_wizard",
        "prompt": "Kurt intake this lot",
        "payload": {
            "draft_contract": {
                "contract": {"type": "ai_agent_draft", "version": 1},
                "agent_key": "kurt_intake_agent",
                "draft_type": "intake",
                "fields": [
                    {"key": "title", "value": "Mixed coin lot", "confidence": 0.9},
                    {"key": "category", "value": "coins", "confidence": 0.8},
                    {"key": "quantity", "value": "", "confidence": 0.0},
                ],
                "missing_questions": [
                    {"field": "quantity", "question": "Confirm Quantity.", "blocking": True},
                ],
                "proposed_actions": [],
                "warnings": [],
                "approval": {"required": True, "status": "pending"},
            },
        },
    }

    suggestions = build_business_room_answer_command_suggestions(handoff)

    assert suggestions == ["kurt answer handoff 88 quantity: 20"]


def test_business_room_answer_command_suggestions_fall_back_to_draft_target():
    handoff = {
        "id": 321,
        "queue_job_id": 0,
        "workflow_key": "listing_wizard",
        "prompt": "Murdock draft listing",
        "payload": {
            "draft_contract": {
                "contract": {"type": "ai_agent_draft", "version": 1},
                "agent_key": "murdock_listing_agent",
                "draft_type": "listing",
                "fields": [
                    {"key": "product_id", "value": 123, "confidence": 1.0},
                    {"key": "title", "value": "Silver Bar", "confidence": 0.9},
                    {"key": "description_html", "value": "<p>Nice</p>", "confidence": 0.9},
                    {"key": "condition_id", "value": "", "confidence": 0.0},
                ],
                "missing_questions": [
                    {"field": "condition_id", "question": "Confirm Condition Id.", "blocking": True},
                ],
                "proposed_actions": [],
                "warnings": [],
                "approval": {"required": True, "status": "pending"},
            },
        },
    }

    suggestions = build_business_room_answer_command_suggestions(handoff)

    assert suggestions == [
        "murdock answer draft 321 condition_id: 3000",
        "murdock answer draft 321 category_id: 261",
        "murdock answer draft 321 suggested_price: 0.00",
    ]


def test_business_room_answer_command_suggestions_include_multiple_missing_fields():
    handoff = {
        "id": 321,
        "queue_job_id": 88,
        "workflow_key": "listing_wizard",
        "prompt": "Murdock draft listing",
        "payload": {
            "draft_contract": {
                "contract": {"type": "ai_agent_draft", "version": 1},
                "agent_key": "murdock_listing_agent",
                "draft_type": "listing",
                "fields": [
                    {"key": "product_id", "value": 123, "confidence": 1.0},
                    {"key": "title", "value": "", "confidence": 0.0},
                    {"key": "description_html", "value": "", "confidence": 0.0},
                ],
                "missing_questions": [
                    {"field": "title", "question": "Confirm Title.", "blocking": True},
                    {"field": "description_html", "question": "Confirm Description.", "blocking": True},
                ],
                "proposed_actions": [],
                "warnings": [],
                "approval": {"required": True, "status": "pending"},
            },
        },
    }

    suggestions = build_business_room_answer_command_suggestions(handoff)

    assert suggestions == [
        "murdock answer handoff 88 title: eBay-safe title",
        "murdock answer handoff 88 description_html: <p>Buyer-facing description.</p>",
        "murdock answer handoff 88 category_id: 261",
    ]


def test_business_room_answer_command_suggestions_include_cost_basis_clarifiers():
    handoff = {
        "id": 321,
        "queue_job_id": 88,
        "workflow_key": "inventory_intake_wizard",
        "prompt": 'Kurt intake "Silver round" qty 3 cost $81.50.',
        "payload": {
            "prompt": 'Kurt intake "Silver round" qty 3 cost $81.50.',
        },
    }

    suggestions = build_business_room_answer_command_suggestions(handoff)

    assert suggestions == [
        "kurt answer handoff 88 product_unit_cost: 0.00",
        "kurt answer handoff 88 lot_landed_total: 0.00",
        "kurt answer handoff 88 assignment_landed_cost: 0.00",
    ]


def test_business_room_answer_command_suggestions_include_listing_readiness_fields():
    handoff = {
        "id": 321,
        "queue_job_id": 88,
        "workflow_key": "listing_wizard",
        "prompt": 'Murdock, create listing for product 198 titled "3 oz Silver Bar".',
        "payload": {
            "prompt": 'Murdock, create listing for product 198 titled "3 oz Silver Bar".',
        },
    }

    suggestions = build_business_room_answer_command_suggestions(handoff, max_suggestions=5)

    assert suggestions == [
        "murdock answer handoff 88 description_html: <p>Buyer-facing description.</p>",
        "murdock answer handoff 88 category_id: 261",
        "murdock answer handoff 88 condition_id: 3000",
        "murdock answer handoff 88 suggested_price: 0.00",
        "murdock answer handoff 88 main_image_id: media_asset_id",
    ]


def test_business_room_answer_command_suggestions_include_item_specifics_example():
    handoff = {
        "id": 321,
        "queue_job_id": 88,
        "workflow_key": "listing_wizard",
        "prompt": 'Murdock, create listing for product 198 titled "3 oz Silver Bar".',
        "payload": {
            "prompt": 'Murdock, create listing for product 198 titled "3 oz Silver Bar".',
        },
    }

    suggestions = build_business_room_answer_command_suggestions(handoff, max_suggestions=8)

    assert "murdock answer handoff 88 item_specifics: Brand=Golden Stackers; Fineness=0.999" in suggestions


def test_business_room_answer_command_suggestions_do_not_duplicate_listing_missing_fields():
    handoff = {
        "id": 321,
        "queue_job_id": 88,
        "workflow_key": "listing_wizard",
        "prompt": "Murdock draft listing for product 198",
        "payload": {
            "draft_contract": {
                "contract": {"type": "ai_agent_draft", "version": 1},
                "agent_key": "murdock_listing_agent",
                "draft_type": "listing",
                "fields": [
                    {"key": "product_id", "value": 198, "confidence": 1.0},
                    {"key": "title", "value": "Silver Bar", "confidence": 0.9},
                ],
                "missing_questions": [
                    {"field": "category_id", "question": "Confirm Category.", "blocking": True},
                ],
                "proposed_actions": [],
                "warnings": [],
                "approval": {"required": True, "status": "pending"},
            },
        },
    }

    suggestions = build_business_room_answer_command_suggestions(handoff, max_suggestions=4)

    assert suggestions == [
        "murdock answer handoff 88 category_id: 261",
        "murdock answer handoff 88 description_html: <p>Buyer-facing description.</p>",
        "murdock answer handoff 88 condition_id: 3000",
        "murdock answer handoff 88 suggested_price: 0.00",
    ]


def test_business_room_action_draft_contract_builds_intake_prompt_hints():
    route = classify_business_room_action_route(
        message='Kurt intake "1881 Morgan Dollar" qty 2 cost $81.50',
        directed_to=["kurt_intake_agent"],
    )

    contract = build_business_room_action_draft_contract(
        message='Kurt intake "1881 Morgan Dollar" qty 2 cost $81.50',
        action_route=route,
        attachments=[{"filename": "coin.jpg"}],
    )

    assert contract["agent_key"] == "kurt_intake_agent"
    assert contract["draft_type"] == "intake"
    values = {row["key"]: row["value"] for row in contract["fields"]}
    assert values["title"] == "1881 Morgan Dollar"
    assert values["quantity"] == 2
    assert values["acquisition_cost"] == "81.50"
    assert values["category"] == "coins"
    assert contract["context"]["attachment_count"] == 1


def test_business_room_action_route_classification():
    listing = classify_business_room_action_route(
        message="Murdock, create an eBay listing draft for product 12",
        directed_to=["murdock_listing_agent"],
    )
    intake = classify_business_room_action_route(
        message="Kurt intake these invoice photos",
        directed_to=[],
    )
    accounting = classify_business_room_action_route(
        message="Goldie update cost basis review notes",
        directed_to=["goldie_accountant_agent"],
    )

    assert listing["route_key"] == "listing"
    assert listing["recommended_workflow"] == "listing_wizard"
    assert intake["route_key"] == "intake"
    assert intake["recommended_workflow"] == "inventory_intake_wizard"
    assert accounting["route_key"] == "accounting"
    assert accounting["recommended_workflow"] == "goldie"


def test_list_business_room_action_requests_parses_queue_payloads():
    class Row:
        id = 88
        status = "blocked"
        requested_by = "keith"
        last_error = "Awaiting approval"
        next_attempt_at = None
        created_at = None
        payload_json = (
            '{"prompt":"Create listing draft","approval":{"status":"pending"},'
            '"action_route":{"route_key":"listing","recommended_workflow":"listing_wizard"},'
            '"directed_to":["murdock_listing_agent"],"source_message_id":55,'
            '"attachments":[{"filename":"coin.jpg"}]}'
        )

    class Repo:
        def list_integration_queue_jobs(self, **kwargs):
            self.kwargs = kwargs
            return [Row()]

    repo = Repo()
    rows = list_business_room_action_requests(repo, environment="prod", statuses={"blocked"}, limit=10)

    assert repo.kwargs["integration"] == "business_chat_room"
    assert repo.kwargs["statuses"] == {"blocked"}
    assert rows[0]["id"] == 88
    assert rows[0]["prompt"] == "Create listing draft"
    assert rows[0]["approval_status"] == "pending"
    assert rows[0]["directed_to"] == ["murdock_listing_agent"]
    assert rows[0]["source_message_id"] == 55
    assert rows[0]["attachment_count"] == 1
    assert rows[0]["route"] == "listing"
    assert rows[0]["route_label"] == "listing"
    assert rows[0]["workflow"] == "listing_wizard"
    assert rows[0]["workflow_page"] == "pages/26_Listing_Wizard.py"
    assert rows[0]["has_draft_contract"] is False
    assert rows[0]["payload"]["prompt"] == "Create listing draft"


def test_business_room_workflow_page_path_maps_known_workflows():
    assert business_room_workflow_page_path("listing_wizard") == "pages/26_Listing_Wizard.py"
    assert business_room_workflow_page_path("inventory_intake_wizard") == "pages/23_Inventory_Intake_Wizard.py"
    assert business_room_workflow_page_path("goldie") == "pages/28_Goldie.py"
    assert business_room_workflow_page_path("unknown") == ""


def test_transition_business_room_action_request_approve_cancel_and_rejects_unknown():
    class Repo:
        def __init__(self):
            self.updates = []

        def update_integration_queue_job(self, job_id, updates, actor):
            self.updates.append({"job_id": job_id, "updates": updates, "actor": actor})
            return type(
                "Row",
                (),
                {
                    "status": updates["status"],
                    "last_error": updates["last_error"],
                },
            )()

    repo = Repo()
    approved = transition_business_room_action_request(
        repo,
        queue_job_id=88,
        transition="approve",
        actor="keith",
    )
    cancelled = transition_business_room_action_request(
        repo,
        queue_job_id=89,
        transition="cancel",
        actor="keith",
    )

    assert approved["status"] == "queued"
    assert cancelled["status"] == "cancelled"
    assert repo.updates[0]["updates"]["last_error"].startswith("Approved")
    assert repo.updates[1]["updates"]["last_error"].startswith("Cancelled")
    with pytest.raises(ValueError, match="Unsupported"):
        transition_business_room_action_request(repo, queue_job_id=90, transition="run", actor="keith")


def test_transition_business_room_action_request_updates_embedded_payload_when_available():
    class Row:
        status = "blocked"
        last_error = ""
        payload_json = json.dumps(
            {
                "approval": {"status": "pending"},
                "execution": {"status": "blocked_pending_approval"},
            }
        )

    class Db:
        def get(self, _model, _row_id):
            return Row()

    class Repo:
        def __init__(self):
            self.db = Db()
            self.updates = []

        def update_integration_queue_job(self, job_id, updates, actor):
            self.updates.append({"job_id": job_id, "updates": updates, "actor": actor})
            return type(
                "UpdatedRow",
                (),
                {
                    "status": updates["status"],
                    "last_error": updates["last_error"],
                },
            )()

    repo = Repo()
    result = transition_business_room_action_request(
        repo,
        queue_job_id=88,
        transition="approve",
        actor="keith",
    )

    assert result["approval_status"] == "approved"
    payload = json.loads(repo.updates[0]["updates"]["payload_json"])
    assert payload["approval"]["status"] == "approved"
    assert payload["approval"]["approved_by"] == "keith"
    assert payload["execution"]["status"] == "queued_after_human_approval"


def test_save_business_room_action_workflow_draft_uses_recommended_workflow():
    class Repo:
        def __init__(self):
            self.saved = []

        def save_workflow_draft(self, **kwargs):
            self.saved.append(kwargs)
            return type("Row", (), {"id": 123})()

    payload = {
        "room_key": "goldenstackers_business",
        "source_message_id": 55,
        "prompt": "Murdock, create a listing draft",
        "requester": {"username": "keith", "role": "admin"},
        "directed_to": ["murdock_listing_agent"],
        "attachments": [{"filename": "coin.jpg"}],
        "action_route": {
            "route_key": "listing",
            "recommended_workflow": "listing_wizard",
        },
        "draft_contract": {
            "contract": {"type": "ai_agent_draft", "version": 1},
            "signature": "listing-contract",
            "agent_key": "murdock_listing_agent",
            "draft_type": "listing",
            "fields": [{"key": "product_id", "value": 12, "confidence": 0.9}],
            "missing_questions": [],
            "proposed_actions": [],
            "warnings": [],
            "approval": {"required": True, "status": "pending"},
        },
        "apply_plan": {"status": "blocked", "reason": "pending_human_approval"},
        "approval": {"status": "pending"},
    }
    repo = Repo()

    result = save_business_room_action_workflow_draft(
        repo,
        environment="prod",
        queue_job_id=88,
        payload=payload,
        actor="qa",
    )

    assert result["draft_id"] == 123
    assert result["workflow_key"] == "listing_wizard"
    assert result["username"] == "keith"
    assert result["scope_key"] == "business_chat_room:88"
    saved = repo.saved[0]
    assert saved["environment"] == "prod"
    assert saved["schema_version"] == "business_room_action_handoff_v1"
    assert saved["last_step"] == "business_room_handoff"
    assert saved["draft_payload"]["queue_job_id"] == 88
    assert saved["draft_payload"]["prompt"] == "Murdock, create a listing draft"
    assert saved["draft_payload"]["draft_contract"]["signature"] == "listing-contract"
    assert saved["draft_payload"]["apply_plan"]["reason"] == "pending_human_approval"


def test_business_room_action_draft_payload_applies_agent_answers_to_contract():
    payload = {
        "room_key": "goldenstackers_business",
        "source_message_id": 55,
        "prompt": "Kurt, intake this lot",
        "requester": {"username": "keith", "role": "admin"},
        "action_route": {
            "route_key": "intake",
            "recommended_workflow": "inventory_intake_wizard",
        },
        "draft_contract": {
            "contract": {"type": "ai_agent_draft", "version": 1},
            "signature": "intake-contract",
            "agent_key": "kurt_intake_agent",
            "draft_type": "intake",
            "fields": [
                {"key": "title", "value": "Mixed coin lot", "confidence": 0.9},
                {"key": "category", "value": "coins", "confidence": 0.8},
                {"key": "quantity", "value": "", "confidence": 0.0},
            ],
            "missing_questions": [
                {"field": "quantity", "question": "Confirm Quantity.", "blocking": True},
            ],
            "proposed_actions": [],
            "warnings": [],
            "approval": {"required": True, "status": "pending"},
        },
        "apply_plan": {"status": "blocked", "reason": "missing_required_confirmations"},
        "ai_agent_answer": {"agent": "kurt", "field": "quantity", "answer": "20"},
    }

    draft_payload = build_business_room_action_draft_payload(queue_job_id=88, payload=payload)

    assert draft_payload["draft_contract"]["operator_answers"][0]["field"] == "quantity"
    assert draft_payload["draft_contract"]["operator_answers"][0]["value"] == "20"
    assert draft_payload["draft_contract"]["fields"][2]["value"] == "20"
    assert draft_payload["draft_contract"]["missing_questions"] == []
    assert draft_payload["apply_plan"]["reason"] == "pending_human_approval"
    assert draft_payload["operator_answers"][0]["field"] == "quantity"


def test_business_room_action_draft_payload_dedupes_repeated_agent_answers():
    payload = {
        "room_key": "goldenstackers_business",
        "source_message_id": 55,
        "prompt": "Kurt, intake this lot",
        "requester": {"username": "keith", "role": "admin"},
        "action_route": {
            "route_key": "intake",
            "recommended_workflow": "inventory_intake_wizard",
        },
        "draft_contract": {
            "contract": {"type": "ai_agent_draft", "version": 1},
            "signature": "intake-contract",
            "agent_key": "kurt_intake_agent",
            "draft_type": "intake",
            "fields": [
                {"key": "title", "value": "Mixed coin lot", "confidence": 0.9},
                {"key": "category", "value": "coins", "confidence": 0.8},
                {"key": "quantity", "value": "", "confidence": 0.0},
            ],
            "missing_questions": [
                {"field": "quantity", "question": "Confirm Quantity.", "blocking": True},
            ],
            "proposed_actions": [],
            "warnings": [],
            "approval": {"required": True, "status": "pending"},
        },
        "ai_agent_answer": {"agent": "kurt", "field": "quantity", "answer": "20"},
        "operator_answers": [
            {"agent": "kurt", "field": "quantity", "answer": "20"},
            {"agent": "kurt", "field": "quantity", "answer": "20"},
        ],
    }

    draft_payload = build_business_room_action_draft_payload(queue_job_id=88, payload=payload)

    assert len(draft_payload["operator_answers"]) == 1
    assert len(draft_payload["draft_contract"]["operator_answers"]) == 1


def test_business_room_action_draft_payload_structures_item_specifics_answers():
    payload = {
        "room_key": "goldenstackers_business",
        "source_message_id": 55,
        "prompt": "Murdock, finish this listing",
        "requester": {"username": "keith", "role": "admin"},
        "action_route": {
            "route_key": "listing",
            "recommended_workflow": "listing_wizard",
        },
        "draft_contract": {
            "contract": {"type": "ai_agent_draft", "version": 1},
            "signature": "listing-contract",
            "agent_key": "murdock_listing_agent",
            "draft_type": "listing",
            "fields": [
                {"key": "product_id", "value": 198, "confidence": 1.0},
                {"key": "item_specifics", "value": "", "confidence": 0.0},
            ],
            "missing_questions": [
                {"field": "item_specifics", "question": "Confirm item specifics.", "blocking": True},
            ],
            "proposed_actions": [],
            "warnings": [],
            "approval": {"required": True, "status": "pending"},
        },
        "ai_agent_answer": {
            "agent": "murdock",
            "field": "item_specifics",
            "answer": '{"Brand":"Golden Stackers","Fineness":"0.999"}',
        },
    }

    draft_payload = build_business_room_action_draft_payload(queue_job_id=88, payload=payload)

    value = draft_payload["draft_contract"]["fields"][1]["value"]
    assert value == {"Brand": "Golden Stackers", "Fineness": "0.999"}
    assert draft_payload["draft_contract"]["operator_answers"][0]["value"] == value
    assert draft_payload["draft_contract"]["missing_questions"] == []
    assert build_business_room_operator_answer_rows(draft_payload) == [
        {
            "field": "item_specifics",
            "answer": '{"Brand":"Golden Stackers","Fineness":"0.999"}',
            "source": "business_chat_room",
            "actor": "keith",
        }
    ]


def test_business_room_action_draft_payload_structures_item_specifics_key_value_answers():
    payload = {
        "room_key": "goldenstackers_business",
        "source_message_id": 55,
        "prompt": "Murdock, finish this listing",
        "requester": {"username": "keith", "role": "admin"},
        "action_route": {
            "route_key": "listing",
            "recommended_workflow": "listing_wizard",
        },
        "draft_contract": {
            "contract": {"type": "ai_agent_draft", "version": 1},
            "signature": "listing-contract",
            "agent_key": "murdock_listing_agent",
            "draft_type": "listing",
            "fields": [
                {"key": "product_id", "value": 198, "confidence": 1.0},
                {"key": "item_specifics", "value": "", "confidence": 0.0},
            ],
            "missing_questions": [
                {"field": "item_specifics", "question": "Confirm item specifics.", "blocking": True},
            ],
            "proposed_actions": [],
            "warnings": [],
            "approval": {"required": True, "status": "pending"},
        },
        "ai_agent_answer": {
            "agent": "murdock",
            "field": "item_specifics",
            "answer": "Brand=Golden Stackers; Fineness=0.999, Precious Metal Content=3 oz",
        },
    }

    draft_payload = build_business_room_action_draft_payload(queue_job_id=88, payload=payload)

    value = draft_payload["draft_contract"]["fields"][1]["value"]
    assert value == {
        "Brand": "Golden Stackers",
        "Fineness": "0.999",
        "Precious Metal Content": "3 oz",
    }
    assert build_business_room_operator_answer_rows(draft_payload) == [
        {
            "field": "item_specifics",
            "answer": '{"Brand":"Golden Stackers","Fineness":"0.999","Precious Metal Content":"3 oz"}',
            "source": "business_chat_room",
            "actor": "keith",
        }
    ]


def test_list_business_room_workflow_handoffs_filters_and_summarizes():
    class Row:
        def __init__(self, row_id, scope_key, payload, username="keith"):
            self.id = row_id
            self.scope_key = scope_key
            self.workflow_key = "listing_wizard"
            self.username = username
            self.status = "active"
            self.created_at = None
            self.updated_at = None
            self.draft_json = payload

    class Repo:
        def list_workflow_drafts(self, **kwargs):
            self.kwargs = kwargs
            return [
                Row(
                    1,
                    "business_chat_room:88",
                    (
                        '{"queue_job_id":88,"prompt":"Murdock, draft this listing",'
                        '"requester":{"username":"keith"},'
                        '"directed_to":["murdock_listing_agent"],'
                        '"attachments":[{"filename":"coin.jpg"}],'
                        '"draft_contract":{"signature":"abc123"},'
                        '"source_message_id":55,'
                        '"action_route":{"route_key":"listing","label":"Listing Draft",'
                        '"next_step":"Review/create a listing draft."}}'
                    ),
                ),
                Row(2, "default", '{"prompt":"normal autosave"}'),
            ]

    repo = Repo()
    rows = list_business_room_workflow_handoffs(
        repo,
        environment="prod",
        workflow_key="listing_wizard",
        username="keith",
        limit=10,
    )

    assert repo.kwargs["environment"] == "prod"
    assert repo.kwargs["workflow_key"] == "listing_wizard"
    assert repo.kwargs["username"] == "keith"
    assert repo.kwargs["active_only"] is True
    assert len(rows) == 1
    assert rows[0]["id"] == 1
    assert rows[0]["queue_job_id"] == 88
    assert rows[0]["prompt"] == "Murdock, draft this listing"
    assert rows[0]["route_label"] == "Listing Draft"
    assert rows[0]["attachment_count"] == 1
    assert rows[0]["directed_to"] == ["murdock_listing_agent"]
    assert rows[0]["payload"]["draft_contract"]["signature"] == "abc123"


def test_list_business_room_active_workflow_handoffs_aggregates_known_workflows():
    class Row:
        def __init__(self, row_id, workflow_key):
            self.id = row_id
            self.scope_key = f"business_chat_room:{row_id}"
            self.workflow_key = workflow_key
            self.username = "keith"
            self.status = "active"
            self.created_at = None
            self.updated_at = None
            self.draft_json = json.dumps(
                {
                    "queue_job_id": row_id,
                    "prompt": f"{workflow_key} prompt",
                    "requester": {"username": "keith"},
                    "action_route": {"route_key": workflow_key, "recommended_workflow": workflow_key},
                }
            )

    class Repo:
        def __init__(self):
            self.calls = []

        def list_workflow_drafts(self, **kwargs):
            self.calls.append(kwargs)
            workflow_key = kwargs["workflow_key"]
            if workflow_key in {"listing_wizard", "inventory_intake_wizard"}:
                return [Row(10 if workflow_key == "listing_wizard" else 11, workflow_key)]
            return []

    repo = Repo()
    rows = list_business_room_active_workflow_handoffs(
        repo,
        environment="prod",
        username="keith",
        workflow_keys=["listing_wizard", "inventory_intake_wizard"],
    )

    assert {row["workflow_key"] for row in rows} == {"listing_wizard", "inventory_intake_wizard"}
    assert {row["workflow_page"] for row in rows} == {
        "pages/26_Listing_Wizard.py",
        "pages/23_Inventory_Intake_Wizard.py",
    }
    assert len(repo.calls) == 2


def test_business_room_agent_workload_summary_groups_actions_and_handoffs():
    rows = build_business_room_agent_workload_summary(
        action_rows=[
            {
                "id": 88,
                "status": "blocked",
                "approval_status": "pending",
                "route": "listing",
                "workflow": "listing_wizard",
                "attachment_count": 1,
                "prompt": "Murdock, create listing for product 198",
                "payload": {
                    "action_route": {"route_key": "listing", "recommended_workflow": "listing_wizard"},
                    "draft_contract": {
                        "agent_key": "murdock_listing_agent",
                        "missing_questions": [
                            {"field": "condition_id", "question": "Confirm condition."},
                        ],
                    },
                },
            },
            {
                "id": 89,
                "status": "queued",
                "approval_status": "approved",
                "route": "intake",
                "workflow": "inventory_intake_wizard",
                "prompt": "Kurt intake this lot",
                "payload": {"action_route": {"route_key": "intake"}},
            },
        ],
        handoff_rows=[
            {
                "id": 12,
                "workflow_key": "inventory_intake_wizard",
                "attachment_count": 2,
                "prompt": "Kurt intake this lot",
                "payload": {
                    "draft_contract": {
                        "agent_key": "kurt_intake_agent",
                        "missing_questions": [
                            {"field": "quantity", "question": "Confirm quantity."},
                        ],
                        "operator_answers": [
                            {"field": "quantity", "answer": "20", "source": "business_chat_room", "actor": "keith"},
                        ],
                    }
                },
            }
        ],
    )

    by_agent = {row["agent_key"]: row for row in rows}
    assert by_agent["murdock_listing_agent"]["pending_action_count"] == 1
    assert by_agent["murdock_listing_agent"]["missing_question_count"] == 1
    assert by_agent["murdock_listing_agent"]["attention"] == "pending_approval"
    assert (
        "murdock answer handoff 88 condition_id: 3000"
        in by_agent["murdock_listing_agent"]["next_answer_commands"]
    )
    assert by_agent["kurt_intake_agent"]["queued_action_count"] == 1
    assert by_agent["kurt_intake_agent"]["active_handoff_count"] == 1
    assert by_agent["kurt_intake_agent"]["missing_question_count"] == 1
    assert by_agent["kurt_intake_agent"]["operator_answer_count"] == 1
    assert by_agent["kurt_intake_agent"]["attachment_count"] == 2
    assert "kurt answer draft 12 quantity: 20" in by_agent["kurt_intake_agent"]["next_answer_commands"]
    assert by_agent["goldie_accountant_agent"]["attention"] == "idle"


def test_business_room_agent_activity_summary_groups_messages_actions_and_handoffs():
    rows = build_business_room_agent_activity_summary(
        recent_messages=[
            {
                "sender_key": "murdock_listing_agent",
                "sender_label": "Murdock",
                "message": "Scout should comp this before I finalize pricing.",
                "directed_to": ["research_pricing_agent"],
                "source": "business_chat_room_ai",
                "created_at_utc": "2026-06-02T12:00:00",
            },
            {
                "sender_key": "keith",
                "sender_label": "keith",
                "message": "Kurt intake this coin lot.",
                "directed_to": ["kurt_intake_agent"],
                "source": "business_chat_room",
                "created_at_utc": "2026-06-02T12:05:00",
            },
        ],
        action_rows=[
            {
                "id": 88,
                "status": "blocked",
                "approval_status": "pending",
                "route": "listing",
                "workflow": "listing_wizard",
                "prompt": "Murdock create listing for product 198",
                "created_at": "2026-06-02T12:10:00",
                "payload": {"action_route": {"route_key": "listing"}},
            }
        ],
        handoff_rows=[
            {
                "id": 12,
                "workflow_key": "inventory_intake_wizard",
                "status": "active",
                "prompt": "Kurt intake this coin lot.",
                "payload": {"draft_contract": {"agent_key": "kurt_intake_agent"}},
            }
        ],
        max_items_per_agent=5,
    )

    by_agent = {row["agent_key"]: row for row in rows}
    assert [item["kind"] for item in by_agent["murdock_listing_agent"]["activity"]] == [
        "action_request",
        "message",
    ]
    assert by_agent["research_pricing_agent"]["activity"][0]["direction"] == "to_agent"
    assert {item["kind"] for item in by_agent["kurt_intake_agent"]["activity"]} == {
        "message",
        "workflow_handoff",
    }
    assert by_agent["goldie_accountant_agent"]["activity_count"] == 0


def test_business_room_agent_focus_summary_combines_workload_and_activity():
    focus = build_business_room_agent_focus_summary(
        agent_key="murdock",
        workload_rows=[
            {
                "agent_key": "murdock_listing_agent",
                "agent": "Murdock",
                "label": "Murdock (Listing + Sales Copy)",
                "attention": "pending_approval",
                "pending_action_count": 1,
                "queued_action_count": 0,
                "failed_action_count": 0,
                "active_handoff_count": 2,
                "missing_question_count": 1,
                "operator_answer_count": 3,
                "attachment_count": 4,
                "latest_prompt": "Draft listing for product 198",
                "latest_workflow": "listing_wizard",
                "next_answer_commands": ["murdock answer handoff 88 condition_id: 3000"],
            }
        ],
        activity_rows=[
            {
                "agent_key": "murdock_listing_agent",
                "activity": [
                    {
                        "kind": "action_request",
                        "status": "pending",
                        "title": "Action request #88",
                        "detail": "Draft listing for product 198",
                    }
                ],
            }
        ],
    )

    assert focus["agent_key"] == "murdock_listing_agent"
    assert focus["agent"] == "Murdock"
    assert focus["role"] == "Listing and Sales Copy Agent"
    assert focus["attention"] == "pending_approval"
    assert focus["pending_action_count"] == 1
    assert focus["active_handoff_count"] == 2
    assert focus["next_answer_commands"] == ["murdock answer handoff 88 condition_id: 3000"]
    assert focus["suggested_prompts"][0] == (
        "Murdock, review pending approvals for Draft listing for product 198."
    )
    assert any("eBay-ready title" in prompt for prompt in focus["suggested_prompts"])
    assert focus["activity"][0]["kind"] == "action_request"


def test_business_room_agent_prompt_board_lists_specialist_prompts():
    rows = build_business_room_agent_prompt_board(
        workload_rows=[
            {
                "agent_key": "goldie_accountant_agent",
                "agent": "Goldie",
                "attention": "needs_answers",
                "latest_prompt": "missing cost basis sale#25",
            }
        ],
        activity_rows=[],
        prompts_per_agent=2,
    )

    by_agent = {row["agent_key"]: row for row in rows}
    assert set(by_agent) == {
        "business_monitor_agent",
        "kurt_intake_agent",
        "murdock_listing_agent",
        "research_pricing_agent",
        "goldie_accountant_agent",
    }
    assert by_agent["goldie_accountant_agent"]["attention"] == "needs_answers"
    assert by_agent["goldie_accountant_agent"]["prompts"][0] == (
        "Goldie, restate the missing confirmations for missing cost basis sale#25."
    )
    assert any("operational priorities" in prompt for prompt in by_agent["business_monitor_agent"]["prompts"])


def test_business_room_agent_prompt_board_uses_customer_rollup():
    rows = build_business_room_agent_prompt_board(
        workload_rows=[],
        activity_rows=[],
        customer_rollup={
            "available": True,
            "repeat_buyer_count": 2,
            "customers_with_internal_notes": 1,
            "dormant_90d_count": 1,
            "top_repeat_buyers": [],
        },
        prompts_per_agent=2,
    )

    by_agent = {row["agent_key"]: row for row in rows}
    assert by_agent["business_monitor_agent"]["prompts"][0] == (
        "Atlas, review repeat-buyer and dormant-customer context and recommend customer follow-up priorities."
    )
    assert by_agent["goldie_accountant_agent"]["prompts"][0] == (
        "Goldie, review customer/repeat-buyer context for accounting or tax-sensitive follow-up risks, using note-presence only."
    )


def test_business_room_standup_brief_summarizes_room_priority():
    brief = build_business_room_standup_brief(
        workload_rows=[
            {
                "agent_key": "murdock_listing_agent",
                "agent": "Murdock",
                "attention": "pending_approval",
                "pending_action_count": 1,
                "queued_action_count": 0,
                "failed_action_count": 0,
                "active_handoff_count": 2,
                "missing_question_count": 1,
                "operator_answer_count": 3,
                "attachment_count": 4,
                "latest_prompt": "Draft listing for product 198",
            },
            {
                "agent_key": "research_pricing_agent",
                "agent": "Scout",
                "attention": "queued",
                "pending_action_count": 0,
                "queued_action_count": 1,
                "failed_action_count": 0,
                "active_handoff_count": 0,
                "missing_question_count": 0,
                "operator_answer_count": 0,
                "attachment_count": 0,
            },
        ],
        prompt_board_rows=[
            {
                "agent_key": "murdock_listing_agent",
                "prompts": ["Murdock, review pending approvals for Draft listing for product 198."],
            },
            {
                "agent_key": "research_pricing_agent",
                "prompts": ["Scout, check queued work."],
            },
        ],
    )

    assert brief["status"] == "pending_approval"
    assert brief["totals"]["pending_approvals"] == 1
    assert brief["totals"]["active_handoffs"] == 2
    assert brief["totals"]["queued"] == 1
    assert brief["totals"]["missing_questions"] == 1
    assert brief["active_agents"][0]["agent"] == "Murdock"
    assert brief["recommended_prompt"] == "Murdock, review pending approvals for Draft listing for product 198."
    assert brief["recommended_prompt_kind"] == "agent_prompt"


def test_business_room_coordination_suggestions_route_listing_to_scout_and_intake_to_goldie():
    rows = build_business_room_coordination_suggestions(
        workload_rows=[
            {
                "agent_key": "murdock_listing_agent",
                "agent": "Murdock",
                "attention": "active_handoff",
                "active_handoff_count": 1,
                "missing_question_count": 0,
                "latest_prompt": "Valcambi 100 gram silver bar listing",
            },
            {
                "agent_key": "kurt_intake_agent",
                "agent": "Kurt",
                "attention": "needs_answers",
                "active_handoff_count": 1,
                "missing_question_count": 1,
                "latest_prompt": "intake coin lot with landed cost evidence",
            },
        ]
    )

    pairs = {(row["source_agent_key"], row["target_agent_key"]) for row in rows}
    assert ("murdock_listing_agent", "research_pricing_agent") in pairs
    assert ("kurt_intake_agent", "goldie_accountant_agent") in pairs
    assert any("sold evidence" in row["prompt"] for row in rows)
    assert any("cost-basis" in row["prompt"] for row in rows)


def test_business_room_coordination_suggestions_include_customer_followup():
    rows = build_business_room_coordination_suggestions(
        workload_rows=[],
        customer_rollup={
            "available": True,
            "repeat_buyer_count": 3,
            "customers_with_internal_notes": 1,
            "dormant_90d_count": 2,
            "top_repeat_buyers": [],
        },
    )

    pairs = {(row["source_agent_key"], row["target_agent_key"]) for row in rows}
    assert ("business_monitor_agent", "business_monitor_agent") in pairs
    assert ("business_monitor_agent", "goldie_accountant_agent") in pairs
    assert any("3 repeat buyer" in row["prompt"] for row in rows)
    assert any("note flags only" in row["prompt"] for row in rows)


def test_business_room_standup_prefers_coordination_prompt_when_available():
    brief = build_business_room_standup_brief(
        workload_rows=[
            {
                "agent_key": "murdock_listing_agent",
                "agent": "Murdock",
                "attention": "active_handoff",
                "pending_action_count": 0,
                "queued_action_count": 0,
                "failed_action_count": 0,
                "active_handoff_count": 1,
                "missing_question_count": 0,
                "operator_answer_count": 0,
                "attachment_count": 0,
                "latest_prompt": "Valcambi bar listing",
            }
        ],
        prompt_board_rows=[
            {
                "agent_key": "murdock_listing_agent",
                "prompts": ["Murdock, summarize the active handoff and what the operator should do next."],
            }
        ],
        coordination_rows=[
            {
                "source_agent_key": "murdock_listing_agent",
                "target_agent_key": "research_pricing_agent",
                "target_agent": "Scout",
                "prompt": "Scout, comp Valcambi bar listing for Murdock.",
                "priority": 20,
            }
        ],
    )

    assert brief["recommended_prompt"] == "Scout, comp Valcambi bar listing for Murdock."
    assert brief["recommended_prompt_kind"] == "coordination"
    assert brief["coordination_count"] == 1


def test_mark_business_room_workflow_handoff_reviewed_clears_and_records_event():
    class Repo:
        def __init__(self):
            self.cleared = []
            self.events = []

        def clear_workflow_draft(self, **kwargs):
            self.cleared.append(kwargs)
            return True

        def append_workflow_event(self, **kwargs):
            self.events.append(kwargs)
            return type("Row", (), {"id": 456})()

    repo = Repo()
    result = mark_business_room_workflow_handoff_reviewed(
        repo,
        environment="prod",
        workflow_key="listing_wizard",
        username="keith",
        actor="keith",
        source="room",
        handoff={
            "id": 123,
            "scope_key": "business_chat_room:88",
            "queue_job_id": 88,
            "source_message_id": 55,
            "route": "listing",
            "payload": {
                "draft_contract": {
                    "signature": "abc",
                    "fields": [{"key": "product_id", "value": 12}],
                }
            },
        },
    )

    assert result["cleared"] is True
    assert result["event_id"] == 456
    assert repo.cleared[0]["reason"] == "business_room_handoff_reviewed_from_room"
    assert repo.events[0]["action"] == "review_business_room_handoff_from_room"
    assert repo.events[0]["payload"]["draft_signature"] == "abc"
    assert repo.events[0]["payload"]["field_count"] == 1


def test_business_room_handoff_review_card_uses_prompt_hints_for_listing():
    card = build_business_room_handoff_review_card(
        {
            "workflow_key": "listing_wizard",
            "prompt": 'Murdock, create listing for product 198 titled "3 oz Silver Bar".',
            "payload": {
                "prompt": 'Murdock, create listing for product 198 titled "3 oz Silver Bar".',
            },
        },
        workflow_key="listing_wizard",
    )

    assert card["draft_type"] == "listing"
    assert card["source"] == "business_room_prompt_hint"
    assert card["field_values"]["product_id"] == 198
    assert card["field_values"]["title"] == "3 oz Silver Bar"
    checks = {row["key"]: row for row in card["listing_readiness_checks"]}
    assert checks["product_link"]["status"] == "ok"
    assert checks["title"]["status"] == "ok"
    assert checks["description"]["status"] == "blocker"
    assert checks["category"]["status"] == "review"


def test_business_room_handoff_review_card_lists_listing_readiness_from_contract():
    draft_contract = {
        "contract": {"type": "ai_agent_draft", "version": 1},
        "signature": "listingabc",
        "agent_key": "murdock_listing_agent",
        "draft_type": "listing",
        "fields": [
            {"key": "product_id", "label": "Product ID", "value": 198, "confidence": 0.9},
            {"key": "title", "label": "Title", "value": "3 oz Silver Bar", "confidence": 0.8},
            {"key": "description_html", "label": "Description", "value": "<p>Ready copy</p>", "confidence": 0.8},
            {"key": "category_id", "label": "Category", "value": "261", "confidence": 0.7},
            {"key": "condition_id", "label": "Condition", "value": "3000", "confidence": 0.7},
            {"key": "suggested_price", "label": "Suggested Price", "value": "199.99", "confidence": 0.7},
            {"key": "item_specifics", "label": "Specifics", "value": {"Brand": "MPM"}, "confidence": 0.7},
        ],
        "missing_questions": [],
        "proposed_actions": [],
        "warnings": [],
        "approval": {"required": True, "status": "pending"},
    }

    card = build_business_room_handoff_review_card(
        {
            "workflow_key": "listing_wizard",
            "attachment_count": 2,
            "prompt": "Murdock, create listing for product 198.",
            "payload": {"draft_contract": draft_contract},
        },
        workflow_key="listing_wizard",
    )

    checks = {row["key"]: row for row in card["listing_readiness_checks"]}
    assert checks["product_link"]["status"] == "ok"
    assert checks["description"]["status"] == "ok"
    assert checks["category"]["status"] == "ok"
    assert checks["condition"]["status"] == "ok"
    assert checks["price"]["status"] == "ok"
    assert checks["media"]["status"] == "ok"
    assert checks["item_specifics"]["status"] == "ok"


def test_business_room_handoff_review_card_uses_contract_fields_first():
    draft_contract = {
        "contract": {"type": "ai_agent_draft", "version": 1},
        "signature": "abc",
        "agent_key": "kurt_intake_agent",
        "draft_type": "intake",
        "fields": [
            {"key": "title", "label": "Title", "value": "Morgan Dollar", "confidence": 0.9},
            {"key": "quantity", "label": "Quantity", "value": 2, "confidence": 0.8},
        ],
        "missing_questions": [],
        "proposed_actions": [],
        "warnings": ["Confirm grade."],
        "approval": {"required": True, "status": "pending"},
    }

    card = build_business_room_handoff_review_card(
        {
            "workflow_key": "inventory_intake_wizard",
            "prompt": 'Kurt intake "Fallback Title" qty 5 cost $20.',
            "payload": {
                "prompt": 'Kurt intake "Fallback Title" qty 5 cost $20.',
                "draft_contract": draft_contract,
            },
        },
        workflow_key="inventory_intake_wizard",
    )

    assert card["draft_type"] == "intake"
    assert card["source"] == "ai_agent_draft_contract"
    assert card["field_values"]["title"] == "Morgan Dollar"
    assert card["field_values"]["quantity"] == 2
    assert card["field_values"]["acquisition_cost"] == "20"
    assert card["warnings"][0] == "Confirm grade."
    assert card["cost_basis_guardrail"]["ambiguous_acquisition_cost"] is True
    assert "acquisition_cost" in {str(row.get("key")) for row in card["fields"]}


def test_business_room_handoff_review_card_labels_explicit_lot_cost_basis():
    card = build_business_room_handoff_review_card(
        {
            "workflow_key": "inventory_intake_wizard",
            "prompt": 'Kurt intake "Mixed coin lot" qty 20 lot landed total $311.05.',
            "payload": {
                "prompt": 'Kurt intake "Mixed coin lot" qty 20 lot landed total $311.05.',
            },
        },
        workflow_key="inventory_intake_wizard",
    )

    assert card["field_values"]["quantity"] == 20
    assert card["field_values"]["lot_landed_total"] == "311.05"
    assert card["cost_basis_guardrail"]["basis_type"] == "lot_landed_total"
    assert card["cost_basis_guardrail"]["requires_confirmation"] is False


def test_business_room_handoff_review_card_flags_ambiguous_intake_cost():
    card = build_business_room_handoff_review_card(
        {
            "workflow_key": "inventory_intake_wizard",
            "prompt": 'Kurt intake "Silver round" qty 3 cost $81.50.',
            "payload": {
                "prompt": 'Kurt intake "Silver round" qty 3 cost $81.50.',
            },
        },
        workflow_key="inventory_intake_wizard",
    )

    assert card["field_values"]["acquisition_cost"] == "81.50"
    assert card["cost_basis_guardrail"]["basis_type"] == "ambiguous_acquisition_cost"
    assert card["cost_basis_guardrail"]["requires_confirmation"] is True
    assert any("Cost basis is ambiguous" in warning for warning in card["warnings"])


def test_record_and_list_business_room_messages_uses_audit_log():
    with in_memory_repo() as (_db, repo):
        created = record_business_room_message(
            repo,
            room_key=DEFAULT_BUSINESS_ROOM_KEY,
            sender_type="user",
            sender_key="keith",
            sender_label="Keith",
            message="Kurt, intake this purchase from Slack photos.",
            directed_to=["kurt_intake_agent"],
            source="app",
            actor="keith",
        )

        assert created["id"] > 0
        rows = list_business_room_messages(repo, room_key=DEFAULT_BUSINESS_ROOM_KEY)
        assert len(rows) == 1
        assert rows[0]["sender_key"] == "keith"
        assert rows[0]["directed_to"] == ["kurt_intake_agent"]

        logs = repo.list_audit_logs(limit=5)
        assert logs[0].entity_type == BUSINESS_CHAT_ENTITY_TYPE
        assert logs[0].action == "message"


def test_business_room_context_snapshot_includes_roster_and_recent_messages():
    with in_memory_repo() as (_db, repo):
        record_business_room_message(
            repo,
            sender_type="agent",
            sender_key="murdock_listing_agent",
            message="I can draft listing copy once Scout has comps.",
            actor="murdock",
        )

        snapshot = build_business_room_context_snapshot(repo)

        assert snapshot["room_key"] == normalize_business_room_key(DEFAULT_BUSINESS_ROOM_KEY)
        assert snapshot["message_count"] == 1
        assert snapshot["recent_messages"][0]["sender_label"] == "Murdock (Listing + Sales Copy)"
        assert any(row["name"] == "Goldie" for row in snapshot["roster"])


def test_business_room_context_snapshot_includes_customer_rollup_without_note_body():
    with in_memory_repo() as (db, repo):
        db.add_all(
            [
                Customer(
                    marketplace="ebay",
                    customer_key="username:repeatbuyer",
                    ebay_username="repeatbuyer",
                    display_name="Repeat Buyer",
                    order_count=3,
                    total_spend=Decimal("123.45"),
                    is_repeat_buyer=True,
                    notes="Prefers combined shipping. Do not expose this note body.",
                    last_order_at=utcnow_naive() - timedelta(days=12),
                ),
                Customer(
                    marketplace="ebay",
                    customer_key="username:dormantbuyer",
                    ebay_username="dormantbuyer",
                    order_count=1,
                    total_spend=Decimal("25.00"),
                    is_repeat_buyer=False,
                    notes="",
                    last_order_at=utcnow_naive() - timedelta(days=120),
                ),
            ]
        )
        db.commit()

        snapshot = build_business_room_context_snapshot(repo)

        rollup = snapshot["customer_rollup"]
        assert rollup["available"] is True
        assert rollup["customer_count"] == 2
        assert rollup["repeat_buyer_count"] == 1
        assert rollup["customers_with_internal_notes"] == 1
        assert rollup["dormant_90d_count"] == 1
        assert rollup["top_repeat_buyers"][0]["identity"] == "repeatbuyer"
        assert rollup["top_repeat_buyers"][0]["has_internal_notes"] is True
        assert rollup["top_dormant_customers"][0]["identity"] == "dormantbuyer"
        assert rollup["top_dormant_customers"][0]["days_since_last_order"] >= 90
        assert "combined shipping" not in json.dumps(rollup)


def test_business_room_message_requires_text():
    with in_memory_repo() as (_db, repo):
        with pytest.raises(ValueError, match="message is required"):
            record_business_room_message(
                repo,
                sender_type="user",
                sender_key="keith",
                message="",
                actor="keith",
            )


def test_record_business_room_turn_records_user_and_agent_messages_in_same_thread():
    with in_memory_repo() as (_db, repo):
        rows = record_business_room_turn(
            repo,
            user_key="keith",
            user_label="Keith",
            agent_key="murdock_listing_agent",
            user_message="Murdock, draft listing copy.",
            agent_message="I need comps before final copy.",
            actor="keith",
            metadata={"intent": "listing_snapshot"},
        )

        assert len(rows) == 2
        assert rows[0]["sender_type"] == "user"
        assert rows[1]["sender_type"] == "agent"
        assert rows[0]["thread_key"] == rows[1]["thread_key"]
        assert rows[1]["sender_label"] == "Murdock (Listing + Sales Copy)"
        assert rows[1]["metadata"]["intent"] == "listing_snapshot"

        messages = list_business_room_messages(repo)
        assert len(messages) == 2
        assert {row["metadata"]["turn_role"] for row in messages} == {"user", "agent"}
