"""The real Facebook adapter inside the real pipeline: Postgres, use cases, Drive download and
the Graph API (faked at the HTTP level). The duplicate-post guarantee, proven for Facebook."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import psycopg
import pytest

from dk_publishing.adapters.media.drive_store import DriveMediaStore
from dk_publishing.adapters.platforms.facebook import FacebookPublisher
from dk_publishing.adapters.platforms.meta import GraphClient
from dk_publishing.adapters.platforms.registry import StaticPublisherRegistry
from dk_publishing.application.services import Services
from dk_publishing.application.use_cases.approve import approve
from dk_publishing.application.use_cases.housekeeping import flag_stale_publishing
from dk_publishing.application.use_cases.prepare import prepare_variant
from dk_publishing.application.use_cases.publish import publish_variant
from dk_publishing.application.use_cases.reconcile import reconcile_variant
from dk_publishing.domain.model import Variant
from tests.integration.conftest import Seed
from tests.integration.rig import ME, SLOT, R, Rig, S
from tests.support import CAPS, MIN
from tests.support.fake_graph import FakeGraph, graph_error
from tests.unit.test_drive_store import VIDEO, FakeDownloader
from tests.unit.test_meta_client import Static

CAPTION = "Kacchi biryani, slow-cooked for Eid and still warm."
MD5 = hashlib.md5(VIDEO).hexdigest()


class LiveRig:
    def __init__(self, conninfo: str, seed: Seed, tmp_path: Path, **drive: bytes) -> None:
        self.rig = Rig(conninfo, seed)
        self.graph = FakeGraph(created=SLOT + timedelta_seconds(20))
        self.downloader = FakeDownloader({"F1": VIDEO, **drive})
        publisher = FacebookPublisher(
            page_id="PAGE",
            capabilities=CAPS,
            graph=GraphClient(version="v25.0", tokens=Static(), transport=self.graph.transport()),
        )
        self.rig.services = replace(
            self.rig.services,
            publishers=StaticPublisherRegistry({"p": publisher}),
            media_store=DriveMediaStore(self.downloader, tmp_path),
        )

    @property
    def services(self) -> Services:
        return self.rig.services

    def approved_video(self, fmt: str = "video", media_md5: str = MD5) -> Variant:
        content = {
            "format": fmt,
            "caption": CAPTION,
            "media": [{"name": "eid.mp4", "drive_file_id": "F1", "md5": media_md5}],
        }
        variant = self.rig.draft()
        assert approve(self.services, variant.id, content, ME)[0] is R.APPROVED
        return self.rig.get(variant.id)

    def prepared_video(self) -> Variant:
        v = self.approved_video()
        assert prepare_variant(self.services, v.id, v.version) is R.PREPARED
        return self.rig.get(v.id)

    def at_slot(self) -> None:
        self.rig.clock.set(SLOT)


def timedelta_seconds(n: int) -> timedelta:
    return timedelta(seconds=n)


@pytest.fixture
def live(conninfo: str, seed: Seed, tmp_path: Path) -> LiveRig:
    return LiveRig(conninfo, seed, tmp_path)


def test_a_video_goes_from_drive_to_the_page_and_the_link_comes_back(
    live: LiveRig, tmp_path: Path
) -> None:
    v = live.prepared_video()
    assert [p.name.split("-", 1)[1] for p in (tmp_path / "originals").iterdir()] == ["eid.mp4"]
    assert live.graph.requests == []  # preparing downloaded the file but never touched Meta

    live.at_slot()
    assert publish_variant(live.services, v.id, v.version) is R.PUBLISHED

    [video] = live.graph.posted("videos")
    assert video["description"] == CAPTION
    assert VIDEO in live.graph.requests[0].content  # the real bytes were uploaded
    status, _, _, external_id, url, published_at = live.rig.row(v.id)
    assert (status, external_id, url) == (
        "published",
        "vid1",
        "https://www.facebook.com/PAGE/videos/1",
    )
    assert published_at == SLOT


def test_the_file_is_downloaded_once_however_often_it_is_prepared(live: LiveRig) -> None:
    v = live.approved_video()
    prepare_variant(live.services, v.id, v.version)
    again = live.rig.get(v.id)
    assert live.downloader.calls == ["F1"] and again.status is S.PREPARED


def test_a_file_replaced_in_drive_before_prepare_is_not_uploaded(live: LiveRig) -> None:
    v = live.approved_video(media_md5="0" * 32)  # what was approved is no longer what Drive holds
    assert prepare_variant(live.services, v.id, v.version) is R.RETRY_SCHEDULED
    assert live.rig.get(v.id).status is S.APPROVED and live.graph.requests == []


# --- the duplicate guarantee, for the real adapter ------------------------------------------------


def test_a_lost_response_after_facebook_made_the_post_is_found_not_reposted(live: LiveRig) -> None:
    v = live.prepared_video()
    live.at_slot()
    live.graph.lose_response_next()  # Facebook publishes, then the answer is lost

    assert publish_variant(live.services, v.id, v.version) is R.FLAGGED_UNKNOWN
    assert len(live.graph.posted("videos")) == 1  # it really is live on the Page

    unknown = live.rig.get(v.id)
    assert reconcile_variant(live.services, v.id, unknown.version) is R.PUBLISHED
    assert len(live.graph.posted("videos")) == 1  # and was never uploaded a second time
    assert live.rig.row(v.id)[3] == "vid1"


def test_a_lost_response_when_facebook_did_nothing_is_published_once_after_reconcile(
    live: LiveRig,
) -> None:
    v = live.prepared_video()
    live.at_slot()
    live.graph.fail_next(httpx.ReadTimeout("no answer"))  # nothing reached Meta
    assert publish_variant(live.services, v.id, v.version) is R.FLAGGED_UNKNOWN
    assert live.graph.posted("videos") == []

    unknown = live.rig.get(v.id)
    assert reconcile_variant(live.services, v.id, unknown.version) is R.NOT_LIVE
    again = live.rig.get(v.id)
    assert publish_variant(live.services, v.id, again.version) is R.PUBLISHED
    assert len(live.graph.posted("videos")) == 1


def test_a_run_that_dies_mid_upload_is_reconciled_by_asking_the_page(live: LiveRig) -> None:
    from tests.support import SimulatedCrash

    v = live.prepared_video()
    live.at_slot()
    live.graph.fail_next(SimulatedCrash())  # the process dies as the request goes out
    with pytest.raises(SimulatedCrash):
        publish_variant(live.services, v.id, v.version)
    assert live.rig.get(v.id).status is S.PUBLISHING

    live.rig.clock.advance(11 * MIN)
    assert flag_stale_publishing(live.services) == [v.id]
    unknown = live.rig.get(v.id)
    assert reconcile_variant(live.services, v.id, unknown.version) is R.NOT_LIVE  # it never arrived
    assert live.graph.posted("videos") == []


# --- what failure looks like to a person ---------------------------------------------------------


def test_an_expired_token_parks_the_post_for_reconnecting_and_posts_nothing(live: LiveRig) -> None:
    v = live.prepared_video()
    live.at_slot()
    live.graph.fail_next(
        graph_error(400, 190, "Error validating access token: Session has expired", 463)
    )
    assert publish_variant(live.services, v.id, v.version) is R.PARKED
    assert live.rig.row(v.id)[:3] == ("prepared", None, None) and live.graph.posted("videos") == []


def test_a_rejection_fails_the_post_with_facebooks_own_words(live: LiveRig) -> None:
    v = live.prepared_video()
    live.at_slot()
    live.graph.fail_next(
        graph_error(
            400, 368, "The action attempted has been deemed abusive or is otherwise disallowed"
        )
    )
    assert publish_variant(live.services, v.id, v.version) is R.FAILED
    assert "deemed abusive" in live.rig.reasons(v.id)[-1]


def test_a_rate_limit_waits_and_then_the_post_goes_out(live: LiveRig) -> None:
    v = live.prepared_video()
    live.at_slot()
    live.graph.fail_next(graph_error(400, 4, "Application request limit reached"))
    assert publish_variant(live.services, v.id, v.version) is R.RETRY_SCHEDULED
    waiting = live.rig.get(v.id)
    assert live.rig.row(v.id)[2] == SLOT + 10 * MIN  # Meta's default pause

    live.rig.clock.set(SLOT + 10 * MIN)
    assert publish_variant(live.services, v.id, waiting.version) is R.PUBLISHED
    assert len(live.graph.posted("videos")) == 1


def test_the_token_never_appears_in_a_stored_failure(live: LiveRig) -> None:
    v = live.prepared_video()
    live.at_slot()
    live.graph.fail_next(graph_error(400, 100, "bad request access_token=EAAB-leaky-token&x=1"))
    publish_variant(live.services, v.id, v.version)
    assert all("EAAB-leaky-token" not in reason for reason in live.rig.reasons(v.id))
    with psycopg_connect(live.rig.conninfo) as conn:
        excerpts = [
            str(r[0] or "")
            for r in conn.execute("SELECT response_excerpt FROM publishing.publish_attempts")
        ]
    assert all("EAAB-leaky-token" not in e for e in excerpts)


def psycopg_connect(conninfo: str) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(conninfo)
