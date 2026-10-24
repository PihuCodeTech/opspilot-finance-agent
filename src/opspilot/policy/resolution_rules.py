"""Lookups over the finding-resolution rules (`04-prd.md` §3)."""

from opspilot.domain.enums import FindingCode, ResolutionType
from opspilot.policy.schema import PolicyConfig, ResolutionRule


def rules_for(policy: PolicyConfig, code: FindingCode) -> tuple[ResolutionRule, ...]:
    return tuple(r for r in policy.resolutions.rules if r.finding is code)


def rule(policy: PolicyConfig, code: FindingCode, kind: ResolutionType) -> ResolutionRule | None:
    return next((r for r in rules_for(policy, code) if r.type is kind), None)


def requires_recheck(policy: PolicyConfig, code: FindingCode) -> bool:
    return code in policy.resolutions.recheck_only
