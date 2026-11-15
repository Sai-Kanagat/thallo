"""The publisher through the real Postiz provider and the registry (fixture transport, no network):
the approved image's bytes are what is uploaded, the approved text is what is posted, and a create
that times out is reconciled from Postiz's own list, with one create call in all."""
# ruff: noqa: F811
from __future__ import annotations

import hashlib
import json
import os

from tests.integration.conftest import pg_urls  # noqa: F401
from tests.integration.test_wave1_flow import IMAGE, backend, shop, signer  # noqa: F401
from thallo.connections import InMemorySecretStore, KeyRing, Vault
from thallo.connections.registry import ConnectionRegistry
from thallo.connections.stores import InMemoryConnectionStore
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.providers.publishing import postiz_hosted
from thallo.providers.publishing.postiz_base import PostizSettings, RawReply, TransportError
from thallo.publish import Publisher, Status

from .harness import build_rig, mirror_connections


class PostizDouble:
    """Plays a Postiz server from the documented shapes: keeps what was posted, lists it back."""

    def __init__(self, *, create_times_out: bool = False) -> None:
        self.create_times_out = create_times_out
        self.uploads: list[bytes] = []
        self.creates: list[dict] = []
        self.posts: list[dict] = []

    def __call__(self, call):
        path = call.url.split("?")[0].rsplit("/", 1)[-1]
        if call.method == "POST" and path == "upload":
            head, _, rest = call.body.partition(b"\r\n\r\n")
            self.uploads.append(rest.rsplit(b"\r\n--", 1)[0])
            return RawReply(200, {"id": f"img-{len(self.uploads)}", "path": "https://uploads.postiz.invalid/x.png"})
        if call.method == "POST" and path == "posts":
            body = json.loads(call.body)
            self.creates.append(body)
            post = body["posts"][0]
            self.posts.append({"id": f"post-{len(self.creates)}", "content": post["value"][0]["content"],
                               "state": "PUBLISHED", "releaseURL": "https://instagram.invalid/p/1",
                               "integration": {"id": post["integration"]["id"]}})
            if self.create_times_out:
                raise TransportError("the exchange failed (TimeoutError)", reached=True)
            return RawReply(200, [{"postId": self.posts[-1]["id"], "integration": post["integration"]["id"]}])
        if call.method == "GET" and path == "posts":
            return RawReply(200, {"posts": self.posts})
        raise AssertionError(f"unexpected {call.method} {path}")


def postiz_rig(backend, shop, signer, double):
    rig = build_rig(backend, shop, signer, fake=False)
    vault = Vault(KeyRing(os.urandom(32)), InMemorySecretStore(), backend.clock)
    providers = rig.registry.providers
    providers.register(Capability.PUBLISHING, postiz_hosted.NAME, ConnectionMode.HOSTED,
                       lambda ctx: postiz_hosted.create(ctx, transport=double), replace=True,
                       settings_schema=PostizSettings)
    registry = ConnectionRegistry(clock=backend.clock, providers=providers, connections=InMemoryConnectionStore(),
                                  vault=vault, environ={})
    registry.add(rig.t, Capability.PUBLISHING, postiz_hosted.NAME, secret="fake-postiz-key-for-tests-only",
                 settings={"integrations": {"instagram": {"id": "ig-1", "provider": "instagram"}}})
    rig.registry = registry
    rig.publisher = Publisher(registry=registry, sessions=rig.sessions, clock=backend.clock, signer=signer)
    mirror_connections(rig)
    return rig


def test_publish_through_postiz_uploads_the_approved_bytes_and_reads_back(backend, shop, signer):
    double = PostizDouble()
    rig = postiz_rig(backend, shop, signer, double)
    out = rig.publish()
    assert out.status is Status.SENT and out.external_id == "post-1"
    assert [hashlib.sha256(u).hexdigest() for u in double.uploads] == list(shop.approved_payload.media_sha256)
    assert double.uploads == [IMAGE]
    assert double.creates[0]["posts"][0]["value"][0]["content"] == shop.approved_payload.text
    assert rig.publisher.readback(rig.t, shop.variant.id).status is Status.READ_BACK_OK


def test_a_postiz_timeout_is_reconciled_from_its_list_with_one_create(backend, shop, signer):
    double = PostizDouble(create_times_out=True)
    rig = postiz_rig(backend, shop, signer, double)
    assert rig.publish().status is Status.NEEDS_CHECK
    assert rig.publish().status is Status.NEEDS_CHECK
    out = rig.publisher.reconcile(rig.t, shop.variant.id)
    assert out.status is Status.SENT and out.external_id == "post-1"
    assert len(double.creates) == 1
