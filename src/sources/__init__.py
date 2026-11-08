from .ashby import AshbySource
from .base import RawPosting, Source, SourceError, load_fixture
from .greenhouse import GreenhouseSource
from .lever import LeverSource
from .oracle import OracleSource
from .workable import WorkableSource
from .smartrecruiters import SmartRecruitersSource
from .workday import WorkdaySource

ADAPTERS: dict[str, Source] = {
    "lever": LeverSource(),
    "greenhouse": GreenhouseSource(),
    "ashby": AshbySource(),
    "smartrecruiters": SmartRecruitersSource(),
    "workday": WorkdaySource(),
    "oracle": OracleSource(),
    "workable": WorkableSource(),
}


def get_source(platform: str) -> Source:
    try:
        return ADAPTERS[platform]
    except KeyError as exc:
        raise SourceError(f"no adapter for platform {platform!r}") from exc
