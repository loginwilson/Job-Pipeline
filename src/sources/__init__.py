from .ashby import AshbySource
from .base import RawPosting, Source, SourceError, load_fixture
from .greenhouse import GreenhouseSource
from .lever import LeverSource
from .smartrecruiters import SmartRecruitersSource

ADAPTERS: dict[str, Source] = {
    "lever": LeverSource(),
    "greenhouse": GreenhouseSource(),
    "ashby": AshbySource(),
    "smartrecruiters": SmartRecruitersSource(),
}


def get_source(platform: str) -> Source:
    try:
        return ADAPTERS[platform]
    except KeyError as exc:
        raise SourceError(f"no adapter for platform {platform!r}") from exc
