from app.services.workflow_contracts import (
    AI_AGENT_DRAFT_CONTRACT_TYPE,
    apply_ai_agent_question_answers,
    build_ai_agent_apply_plan,
    build_ai_agent_draft_payload,
    build_ai_agent_missing_questions,
    extract_ai_agent_draft_payload,
    parse_ai_agent_answer_prompt,
)


def test_build_ai_agent_draft_payload_normalizes_fields_and_missing_questions():
    payload = build_ai_agent_draft_payload(
        agent_key="kurt_intake_agent",
        draft_type="intake",
        operator_request="kurt 1881 morgan dollar",
        fields=[
            {"key": "title", "value": "1881 Morgan Dollar", "confidence": 88, "source": "operator"},
            {"key": "category", "value": "coins", "confidence": 0.8, "source": "operator"},
            {"key": "quantity", "value": "", "confidence": 0.0, "source": "missing"},
        ],
        proposed_actions=[{"action": "review_intake_product", "requires_approval": True}],
    )

    assert payload["contract"]["type"] == AI_AGENT_DRAFT_CONTRACT_TYPE
    assert payload["agent_key"] == "kurt_intake_agent"
    assert payload["fields"][0]["confidence"] == 0.88
    assert payload["signature"]
    assert payload["missing_questions"][0]["field"] == "quantity"

    extracted = extract_ai_agent_draft_payload(payload)
    assert extracted["is_contract"] is True
    assert extracted["field_values"]["title"] == "1881 Morgan Dollar"
    assert extracted["proposed_actions"][0]["action"] == "review_intake_product"


def test_ai_agent_apply_plan_blocks_until_questions_answered_and_approved():
    blocked_payload = build_ai_agent_draft_payload(
        agent_key="murdock_listing_agent",
        draft_type="listing",
        fields=[
            {"key": "product_id", "value": 123, "confidence": 1.0},
            {"key": "title", "value": "Silver Bar", "confidence": 0.9},
            {"key": "description_html", "value": "", "confidence": 0.0},
        ],
        proposed_actions=[{"action": "update_listing_draft", "requires_approval": True}],
    )
    blocked = build_ai_agent_apply_plan(blocked_payload)
    assert blocked["safe_to_apply"] is False
    assert blocked["reason"] == "missing_required_confirmations"

    ready_payload = build_ai_agent_draft_payload(
        agent_key="murdock_listing_agent",
        draft_type="listing",
        fields=[
            {"key": "product_id", "value": 123, "confidence": 1.0},
            {"key": "title", "value": "Silver Bar", "confidence": 0.9},
            {"key": "description_html", "value": "<p>Nice silver bar.</p>", "confidence": 0.9},
        ],
        proposed_actions=[{"action": "update_listing_draft", "requires_approval": True}],
    )
    pending = build_ai_agent_apply_plan(ready_payload)
    assert pending["safe_to_apply"] is False
    assert pending["reason"] == "pending_human_approval"

    ready_payload["approval"]["status"] = "approved"
    ready = build_ai_agent_apply_plan(ready_payload)
    assert ready["safe_to_apply"] is True
    assert ready["reason"] == "ready"


def test_missing_questions_can_use_custom_required_fields():
    questions = build_ai_agent_missing_questions(
        draft_type="listing",
        required_fields=["condition_id"],
        fields=[{"key": "condition_id", "value": None, "confidence": 0.0}],
    )

    assert questions == [
        {
            "field": "condition_id",
            "question": "Confirm Condition Id.",
            "reason": "missing_or_low_confidence",
            "confidence": 0.0,
            "blocking": True,
        }
    ]


def test_parse_ai_agent_answer_prompt_supports_concise_agent_replies():
    parsed = parse_ai_agent_answer_prompt("Kurt answer lot landed total: 311.05")

    assert parsed["agent"] == "kurt"
    assert parsed["field"] == "lot_landed_total"
    assert parsed["answer"] == "311.05"
    assert parsed["target_queue_job_id"] == 0
    assert parsed["target_draft_id"] == 0


def test_parse_ai_agent_answer_prompt_supports_targeted_handoff_replies():
    parsed = parse_ai_agent_answer_prompt("Murdock answer handoff 88 condition id: 3000")

    assert parsed["agent"] == "murdock"
    assert parsed["field"] == "condition_id"
    assert parsed["answer"] == "3000"
    assert parsed["target_queue_job_id"] == 88
    assert parsed["target_draft_id"] == 0


def test_parse_ai_agent_answer_prompt_supports_targeted_draft_replies():
    parsed = parse_ai_agent_answer_prompt("Murdock answer draft #321 condition id: 3000")

    assert parsed["field"] == "condition_id"
    assert parsed["target_queue_job_id"] == 0
    assert parsed["target_draft_id"] == 321


def test_apply_ai_agent_question_answers_updates_contract_and_remaining_questions():
    payload = build_ai_agent_draft_payload(
        agent_key="kurt_intake_agent",
        draft_type="intake",
        fields=[
            {"key": "title", "value": "Mixed coin lot", "confidence": 0.9},
            {"key": "category", "value": "coins", "confidence": 0.8},
            {"key": "quantity", "value": "", "confidence": 0.0},
        ],
    )

    updated = apply_ai_agent_question_answers(
        payload,
        [{"field": "quantity", "answer": "20"}],
        actor="keith",
        source="slack",
    )
    extracted = extract_ai_agent_draft_payload(updated)

    assert extracted["field_values"]["quantity"] == "20"
    assert extracted["missing_questions"] == []
    assert updated["operator_answers"][0]["actor"] == "keith"
    assert updated["operator_answers"][0]["source"] == "slack"
    assert updated["signature"] != payload["signature"]


def test_apply_ai_agent_question_answers_dedupes_repeated_answers():
    payload = build_ai_agent_draft_payload(
        agent_key="murdock_listing_agent",
        draft_type="listing",
        fields=[
            {"key": "product_id", "value": 123, "confidence": 1.0},
            {"key": "title", "value": "Silver Bar", "confidence": 0.9},
            {"key": "description_html", "value": "<p>Nice.</p>", "confidence": 0.9},
            {"key": "condition_id", "value": "", "confidence": 0.0},
        ],
        missing_questions=[
            {"field": "condition_id", "question": "Confirm Condition Id.", "blocking": True},
        ],
    )

    first = apply_ai_agent_question_answers(
        payload,
        [{"field": "condition_id", "answer": "3000"}],
        actor="keith",
        source="slack",
    )
    second = apply_ai_agent_question_answers(
        first,
        [{"field": "condition_id", "answer": "3000"}],
        actor="keith",
        source="slack",
    )
    extracted = extract_ai_agent_draft_payload(second)
    condition = [row for row in extracted["fields"] if row["key"] == "condition_id"][0]

    assert extracted["field_values"]["condition_id"] == "3000"
    assert len(second["operator_answers"]) == 1
    assert len(condition["evidence"]) == 1


def test_apply_ai_agent_question_answers_moves_approved_draft_back_to_pending():
    payload = build_ai_agent_draft_payload(
        agent_key="murdock_listing_agent",
        draft_type="listing",
        fields=[
            {"key": "product_id", "value": 123, "confidence": 1.0},
            {"key": "title", "value": "Silver Bar", "confidence": 0.9},
            {"key": "description_html", "value": "<p>Nice.</p>", "confidence": 0.9},
        ],
    )
    payload["approval"]["status"] = "approved"

    updated = apply_ai_agent_question_answers(
        payload,
        [{"field": "condition_id", "answer": "3000"}],
        actor="keith",
    )

    assert updated["approval"]["status"] == "pending"
    assert updated["approval"]["reason"] == "operator_answers_changed_draft"
    assert extract_ai_agent_draft_payload(updated)["field_values"]["condition_id"] == "3000"
