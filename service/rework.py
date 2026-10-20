"""Relay uses the same attempt CAS as initial submission."""
import json

from adapters.base import AdapterResult
from contracts.schemas import ContractError


class ReworkSubmissionAdapter:
    def __init__(self, adapter, review):
        self.adapter = adapter
        self.review = review

    def submit(self, task, attempt):
        try:
            receipt = self.adapter.send_decision(task, self.review, attempt)
        except RuntimeError as exc:
            raise ContractError("submit_unknown", message="返工提交结果未知，不重发：%s" % exc)
        return AdapterResult(
            status="accepted_by_host", source_id=task["source_id"],
            host_receipt=receipt["runId"], evidence_level=receipt["evidenceLevel"],
            note=json.dumps({"relayReviewId": self.review["review_id"],
                             "receipt": receipt}, sort_keys=True),
        )


def relay_receipt(submission, review_id):
    try:
        detail = json.loads(submission["note"])
        if detail["relayReviewId"] == review_id:
            return detail["receipt"]
    except (KeyError, ValueError, TypeError):
        pass
    raise ContractError("invalid_transition", message="新轮次已经通过其他入口提交，不能再次发送返工")
