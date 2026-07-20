from repogent.approvals import FakeApprover
from repogent.domain import ApprovalKind, Decision


def test_fake_approver_records_ordered_decisions() -> None:
    approver = FakeApprover([Decision.APPROVED, Decision.REJECTED])
    first = approver.decide(ApprovalKind.REQUIREMENTS, "requirements")
    second = approver.decide(ApprovalKind.PLAN, "plan")
    assert first.decision is Decision.APPROVED
    assert second.decision is Decision.REJECTED
    assert [record.kind for record in approver.records] == [
        ApprovalKind.REQUIREMENTS,
        ApprovalKind.PLAN,
    ]
