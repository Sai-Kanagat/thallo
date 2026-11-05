from datetime import datetime

from cortex_operator.contracts import Item
from cortex_operator.postiz import PostizMetrics, PostizPublisher
from cortex_operator.publishing import deliver

MAP = {"linkedin": {"id": "ch1", "type": "linkedin"}}


def item(url=None):
    return Item("a", "s", "linkedin", "b", datetime(2026, 11, 16), text="Hi [[C-0001]].", url=url)


class Fake:
    def __init__(self, error=None):
        self.bodies, self.error = [], error

    def __call__(self, method, path, body=None):
        if self.error:
            raise self.error
        self.bodies.append(body)
        return [{"postId": "p9", "integration": "ch1"}]


def test_prelaunch_only_creates_a_draft(tmp_path):
    fake = Fake()
    url = deliver(PostizPublisher(fake, MAP, False, tmp_path / "s.json"), "k", item())
    assert fake.bodies[0]["type"] == "draft" and url == "postiz://post/p9"
    assert fake.bodies[0]["posts"][0]["value"][0]["content"] == "Hi."


def test_live_mode_publishes_and_survives_restart_without_double_post(tmp_path):
    fake, path = Fake(), tmp_path / "s.json"
    deliver(PostizPublisher(fake, MAP, True, path), "k", item())
    deliver(PostizPublisher(fake, MAP, True, path), "k", item())
    assert fake.bodies[0]["type"] == "now" and len(fake.bodies) == 1


def test_timeout_is_not_retried(tmp_path):
    calls = []

    def call(*args):
        calls.append(args)
        raise TimeoutError

    assert deliver(PostizPublisher(call, MAP, True, tmp_path / "s.json"), "k", item()) is None
    assert len(calls) == 1


def test_refusal_and_unknown_channel_fail_safely(tmp_path):
    assert deliver(PostizPublisher(Fake(ValueError("no")), MAP, True, tmp_path / "s"), "k", item()) is None
    assert deliver(PostizPublisher(Fake(), {}, True, tmp_path / "t"), "k", item()) is None


def test_metrics_are_normalised():
    rows = [{"label": "Likes", "data": [{"total": "1"}, {"total": "175"}]}, {"label": "X", "data": []}]
    assert PostizMetrics(lambda *a: rows).pull(item("postiz://post/p9")) == {"likes": 175.0}
    assert PostizMetrics(lambda *a: rows).pull(item()) == {}
