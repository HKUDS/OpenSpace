"""Record how a saved hypothesis's check affects selection, without a judge model."""
from agentpilot.domain import stable_id

REVIEW_SCHEMA = {
    "type":"object",
    "description":"Report the check of the saved draft and its effect on your final choice. This is your assessment, not certified effectiveness.",
    "properties":{
        "outcome":{"type":"string", "enum":["not_refuted", "refuted", "inconclusive"]},
        "effect":{"type":"string", "enum":["retain", "revise"]},
        "observation":{"type":"string", "minLength":1, "maxLength":2048,
                       "description":"What actually happened or was established, and why the final mechanism or applicability is retained or revised. Separate measured results from predictions."},
    },
    "required":["outcome", "effect", "observation"],
    "additionalProperties":False,
}


def intent_fingerprint(direction):
    return stable_id("counterexample-intent", direction.observed_behavior,
                     direction.failure_mechanism, direction.implementation_direction,
                     direction.declared_scope, direction.new_paths)


def review_issues(review, *, revised=None):
    def issue(code, message):
        return [{"field":"mechanism_test.review", "code":code, "message":message}]
    if (not isinstance(review, dict) or set(review) != {"outcome", "effect", "observation"}
            or not isinstance(review.get("outcome"), str)
            or not isinstance(review.get("effect"), str)
            or review.get("outcome") not in {"not_refuted", "refuted", "inconclusive"}
            or review.get("effect") not in {"retain", "revise"}
            or not isinstance(review.get("observation"), str)
            or not 1 <= len(review["observation"].strip()) <= 2048):
        return issue("missing_counterexample_review", "Report review.outcome, effect and observation after checking the saved draft; unknown results may be inconclusive.")
    if review["outcome"] == "refuted" and review["effect"] == "retain":
        return issue("refuted_but_retained", "A refuted draft cannot be retained as the selected mechanism. Revise/narrow it, or use withdraw_draft; the original remains saved.")
    if review["effect"] == "revise" and revised is False:
        return issue("unchanged_revision", "The proposed mechanism/applicability is unchanged. New citations or test prose alone are not a revision; revise the direction or report an honest retained/inconclusive result.")
    return []
