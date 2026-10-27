"""Which actions may be proposed for a case (`04-prd.md` §3).

A case's effective list is the intersection of the lists for its *unresolved*
findings. Payment is possible only with no findings, or (for analysts) when
every finding is resolved and none is recheck-only. Agents propose before any
resolution exists, so for them resolved findings still restrict (fail closed).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from functools import reduce

from opspilot.domain.enums import RESTRICTED_CODES, ActionType, FindingCode, ProposalSource
from opspilot.policy.schema import Category, PolicyConfig


@dataclass(frozen=True)
class FindingState:
    code: FindingCode
    resolved: bool = False


def category_of(code: FindingCode) -> Category:
    if code is FindingCode.EXTRACTION_UNCERTAIN:
        return "inconclusive"
    if code in RESTRICTED_CODES:
        return "restricted"
    return "other"


def allowed_actions(
    policy: PolicyConfig, actor: ProposalSource, findings: Iterable[FindingState]
) -> frozenset[ActionType]:
    lists = (
        policy.allowlists.analyst if actor is ProposalSource.ANALYST else policy.allowlists.agent
    )
    recheck_only = policy.resolutions.recheck_only
    no_resolution = policy.resolutions.no_resolution_needed

    relevant = [f for f in findings if f.code not in no_resolution]
    if not relevant:
        return lists.clean

    unresolved = [f for f in relevant if not f.resolved or f.code in recheck_only]
    if not unresolved and actor is ProposalSource.ANALYST:
        return policy.allowlists.analyst.all_resolved

    restricting = unresolved or relevant  # agent: resolutions never loosen its list
    categories = {category_of(f.code) for f in restricting}
    allowed: frozenset[ActionType] = reduce(
        frozenset.intersection, (getattr(lists, c) for c in categories)
    )
    # The schema guarantees place_on_hold in every non-clean list, so this never empties;
    # fail closed regardless.
    return allowed or frozenset({ActionType.PLACE_ON_HOLD})
