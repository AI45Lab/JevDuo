"""Share a proven pointer preflight refusal only within the unchanged UI."""

POINTER_OPERATIONS = ("POINTER_CLICK", "RIGHT_CLICK", "DOUBLE_CLICK", "TRIPLE_CLICK", "HOVER")


def shared_receiver_refusal(outcome, operation):
    error = outcome.get("error") or {}
    if (
        operation not in POINTER_OPERATIONS[:-1]
        or outcome.get("delivery") != "not_delivered"
        or error.get("disposition") != {"delivery": "not_delivered", "retry": "safe"}
        or error.get("code") not in {"ACTION_FAILED", "TIMEOUT"}
    ):
        return False
    proof = error.get("details") or {}
    if proof.get("kind") == "actionability_timeout":
        last = proof.get("last_report") or {}
        if last.get("phase") != "preflight":
            return False
        proof = last.get("report") or {}
    if proof.get("physical_delivery_started") is not False:
        return False
    if proof.get("kind") == "pointer_hit_unproven":
        clicks = proof.get("delivered_clicks")
        return proof.get("hit_test") == "Unknown" and type(clicks) is int and clicks == 0
    return (
        proof.get("kind") == "hit_test_unproven"
        and proof.get("reason") in {"sibling_overlap", "child_bounds_unknown"}
    )
