from __future__ import annotations

from collections.abc import Iterable, Mapping

LISTING_DRAFT_CONTRACT_TYPE = "listing_draft"
LISTING_DRAFT_CONTRACT_VERSION = 1


def build_listing_draft_payload(
    *,
    state: Mapping[str, object] | None = None,
    context: Mapping[str, object] | None = None,
    signature: str = "",
) -> dict[str, object]:
    state_obj = dict(state or {})
    context_obj = dict(context or {})
    return {
        "contract": {
            "type": LISTING_DRAFT_CONTRACT_TYPE,
            "version": LISTING_DRAFT_CONTRACT_VERSION,
        },
        "signature": str(signature or "").strip(),
        "context": context_obj,
        "state": state_obj,
    }


def extract_listing_draft_payload(
    payload: Mapping[str, object] | None,
    *,
    state_keys: Iterable[str] | None = None,
    context_keys: Iterable[str] | None = None,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        return {
            "is_contract": False,
            "contract_version": 0,
            "signature": "",
            "state": {},
            "context": {},
        }

    raw_contract = payload.get("contract")
    contract_obj = raw_contract if isinstance(raw_contract, Mapping) else {}
    is_contract = str(contract_obj.get("type") or "").strip() == LISTING_DRAFT_CONTRACT_TYPE
    try:
        contract_version = int(contract_obj.get("version") or 0)
    except Exception:
        contract_version = 0
    signature = str(payload.get("signature") or "").strip()

    allowed_state_keys = {str(key) for key in (state_keys or []) if str(key or "").strip()}
    allowed_context_keys = {str(key) for key in (context_keys or []) if str(key or "").strip()}

    nested_state = payload.get("state")
    if isinstance(nested_state, Mapping):
        state_source = nested_state
    else:
        state_source = payload
    if allowed_state_keys:
        state_obj = {str(key): state_source.get(str(key)) for key in allowed_state_keys if str(key) in state_source}
    else:
        state_obj = dict(state_source)

    nested_context = payload.get("context")
    if isinstance(nested_context, Mapping):
        context_source = nested_context
    else:
        context_source = payload
    if allowed_context_keys:
        context_obj = {
            str(key): context_source.get(str(key))
            for key in allowed_context_keys
            if str(key) in context_source
        }
    else:
        context_obj = dict(context_source)

    if not signature:
        signature = str(context_obj.get("listing_signature") or payload.get("listing_signature") or "").strip()

    return {
        "is_contract": is_contract,
        "contract_version": contract_version,
        "signature": signature,
        "state": state_obj,
        "context": context_obj,
    }

