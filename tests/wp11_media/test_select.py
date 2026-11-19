from __future__ import annotations

from datetime import timedelta

import pytest

from thallo.connections.errors import InvalidConnection
from thallo.contracts.enums import Capability, Channel, Language, MediaKind, MediaProvenance
from thallo.contracts.models import Brief, Media
from thallo.media.select import image_plan, piece_override, rank, video_plan, voiceover_request

from .conftest import T

ON = {"THALLO_ALLOW_GENERATED_MEDIA": "1"}


def media(clock, prov, i, kind=MediaKind.IMAGE):
    return Media(id=f"0190f3a0-0000-7000-8000-00000000010{i}", created_at=clock.now() + timedelta(seconds=i),
                 tenant_id=T, kind=kind, sha256=f"{i}" * 64, mime="image/png", size_bytes=1, storage_uri="local:x",
                 provenance=prov)


def test_real_photos_first_then_renders_never_generated(clock):
    gen, render, photo = (media(clock, MediaProvenance.GENERATED, 1), media(clock, MediaProvenance.TEMPLATE_RENDER, 2),
                          media(clock, MediaProvenance.CLIENT_PHOTO, 3))
    assert [m.provenance for m in rank([gen, render, photo], kind=MediaKind.IMAGE)] == [
        MediaProvenance.CLIENT_PHOTO, MediaProvenance.TEMPLATE_RENDER]
    plan = image_plan(tenant_id=T, width=10, height=10, candidates=[gen, render, photo], template_ref="quote-01",
                      prompt="paper", format_allows_generated=True, environ=ON)
    assert plan.use == (photo,) and plan.requests == () and not plan.flagged


def test_no_photo_means_template_and_generated_only_behind_switch_and_format(clock):
    off = image_plan(tenant_id=T, width=10, height=10, template_ref="quote-01", prompt="paper",
                     format_allows_generated=True, environ={})
    assert [r.purpose for r in off.requests] == ["template_render"] and not off.flagged
    fmt_off = image_plan(tenant_id=T, width=10, height=10, template_ref="quote-01", prompt="paper", environ=ON)
    assert [r.purpose for r in fmt_off.requests] == ["template_render"]
    on = image_plan(tenant_id=T, width=10, height=10, template_ref="quote-01", prompt="paper",
                    format_allows_generated=True, environ=ON)
    assert [r.purpose for r in on.requests] == ["template_render", "background"] and on.flagged


def test_video_cuts_real_clips_first(clock):
    clip = media(clock, MediaProvenance.CLIENT_PHOTO, 4, kind=MediaKind.VIDEO)
    plan = video_plan(tenant_id=T, width=10, height=10, candidates=[clip], script="hello")
    assert plan.requests[0].action == "caption" and plan.requests[0].input_sha256 == (clip.sha256,)
    none = video_plan(tenant_id=T, width=10, height=10, script="hello", environ={})
    assert [r.action for r in none.requests] == ["template_render"]


def test_voiceover_per_brief(clock):
    def brief(channels):  # noqa: ANN001
        return Brief(id="0190f3a0-0000-7000-8000-000000000201", created_at=clock.now(), tenant_id=T,
                     idea_id="0190f3a0-0000-7000-8000-000000000202", audience="a", job_to_be_done="j",
                     message="Fresh bread at seven", channels=channels, call_to_action="Visit", language=Language.IT)
    assert voiceover_request(brief([Channel.EMAIL])) is None
    v = voiceover_request(brief([Channel.INSTAGRAM]))
    assert v.text == "Fresh bread at seven" and v.language is Language.IT and not v.is_real_person_voice


def test_piece_override_is_piece_scoped_and_respects_the_switch(registry):
    with pytest.raises(InvalidConnection, match="switched off"):
        piece_override(registry, tenant_id=T, piece_id="p1", capability=Capability.IMAGE,
                       provider="generated_hosted", model="m", environ={})
    row = piece_override(registry, tenant_id=T, piece_id="p1", capability=Capability.IMAGE,
                         provider="template_render")
    assert row.scope == "piece" and row.scope_ref == "p1"
    assert registry.resolve(T, Capability.IMAGE, piece_id="p1").ordered_connection_ids[0] == row.id
    assert registry.resolve(T, Capability.IMAGE).ordered_connection_ids[0] != row.id
    assert piece_override(registry, tenant_id=T, piece_id="p1", capability=Capability.IMAGE,
                          provider="template_render").id == row.id
