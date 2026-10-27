"""Load and fingerprint the policy configuration."""

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from opspilot.domain.digest import sha256_hex
from opspilot.policy.schema import PolicyConfig

DEFAULT_POLICY_PATH = Path(__file__).with_name("config.toml")


@dataclass(frozen=True)
class LoadedPolicy:
    config: PolicyConfig
    config_sha256: str  # digest of the validated values, independent of file formatting

    @property
    def version(self) -> str:
        return self.config.version


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> LoadedPolicy:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    config = PolicyConfig.model_validate(raw)
    return LoadedPolicy(config=config, config_sha256=sha256_hex(config.model_dump()))


@lru_cache(maxsize=1)
def active_policy() -> LoadedPolicy:
    return load_policy()
