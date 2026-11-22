from sqlalchemy import select

from actionflow.persistence.database import utc_now
from actionflow.persistence.findings import Finding, FindingProposal


def ensure_proposal(
    db,
    finding: Finding,
    *,
    kind: str,
    reason: dict,
    source_key: str,
    run_id: str | None = None,
) -> FindingProposal:
    existing = db.scalar(
        select(FindingProposal).where(
            FindingProposal.finding_id == finding.id,
            FindingProposal.kind == kind,
            FindingProposal.source_key == source_key,
        )
    )
    if existing is not None:
        return existing
    proposal = FindingProposal(
        finding_id=finding.id,
        run_id=run_id,
        kind=kind,
        status="open",
        reason=reason,
        source_key=source_key,
        created_at=utc_now(),
    )
    db.add(proposal)
    db.flush()
    return proposal
