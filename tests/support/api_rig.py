"""Real platform adapters (validating, talking to fakes) behind the API's services."""

from __future__ import annotations

from pathlib import Path

from dk_publishing.adapters.media.public import PublicMediaStore
from dk_publishing.adapters.media.uploads import UploadStore
from dk_publishing.adapters.persistence.unit_of_work import PostgresUnitOfWork
from dk_publishing.adapters.platforms.facebook import FacebookPublisher
from dk_publishing.adapters.platforms.guides import GUIDES
from dk_publishing.adapters.platforms.instagram import InstagramPublisher
from dk_publishing.adapters.platforms.linkedin import build_linkedin
from dk_publishing.adapters.platforms.meta import GraphClient
from dk_publishing.adapters.platforms.registry import StaticPublisherRegistry
from dk_publishing.adapters.platforms.threads import HOST, ThreadsPublisher
from dk_publishing.adapters.platforms.tiktok import build_tiktok
from dk_publishing.adapters.platforms.youtube import YouTubePublisher
from dk_publishing.application.ports import Publisher
from dk_publishing.application.services import ApiServices, Services
from tests.support import CAPS, NATIVE_CAPS, FakeClock
from tests.support.fake_graph import TOKEN, FakeGraph
from tests.support.fake_instagram import FakeInstagram
from tests.support.fake_telegram import FakeNotifier
from tests.support.fake_threads import FakeThreads
from tests.support.fake_tiktok import MemoryLog
from tests.support.fake_youtube import FakeYouTube

BASE = "https://media.example.test/m"
MEDIA_PLACEHOLDER = "MEDIA_ID"


class _Token:
    def token(self) -> str:
        return TOKEN


def publishers(tmp_path: Path, notifier: FakeNotifier | None = None) -> dict[str, Publisher]:
    public = PublicMediaStore(tmp_path / "public", BASE)
    ig = FakeInstagram()
    th = FakeThreads()
    return {
        "instagram": InstagramPublisher(
            account_id="IG1",
            capabilities=CAPS,
            graph=GraphClient(version="v25.0", tokens=_Token(), transport=ig.transport()),
            version="v25.0",
            public=public,
            sleep=lambda _: None,
        ),
        "facebook": FacebookPublisher(
            page_id="PAGE",
            capabilities=NATIVE_CAPS,
            graph=GraphClient(version="v25.0", tokens=_Token(), transport=FakeGraph().transport()),
        ),
        "threads": ThreadsPublisher(
            user_id="TH1",
            capabilities=CAPS,
            graph=GraphClient(
                version="v1.0",
                tokens=_Token(),
                transport=th.transport(),
                host=HOST,
                video_host=HOST,
            ),
            public=public,
            sleep=lambda _: None,
        ),
        "youtube": YouTubePublisher(
            api=FakeYouTube(), capabilities=NATIVE_CAPS, sleep=lambda _: None
        ),
        "tiktok": build_tiktok(CAPS, notifier or FakeNotifier(), MemoryLog()),
        "linkedin": build_linkedin(CAPS, notifier or FakeNotifier(), MemoryLog()),
    }


def api_services(
    conninfo: str,
    tmp_path: Path,
    clock: FakeClock,
    notifier: FakeNotifier | None = None,
    **upload: object,
) -> ApiServices:
    built = publishers(tmp_path, notifier)
    return ApiServices(
        core=Services(
            uow=lambda: PostgresUnitOfWork(conninfo),
            publishers=StaticPublisherRegistry(built),
            clock=clock,
        ),
        guides={name: GUIDES[name] for name in built},
        modes={name: "live" for name in built},
        intake=UploadStore(tmp_path / "media", **upload),  # type: ignore[arg-type]
        notifier=notifier,
    )
