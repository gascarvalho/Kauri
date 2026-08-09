"""Source contract for replica shutdown diagnostics used by live campaigns."""

from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[3]
APP_SOURCE = REPOSITORY / "examples/hotstuff_app.cpp"


def _function(source: str, signature: str, next_signature: str) -> str:
    start = source.index(signature)
    end = source.index(next_signature, start)
    return source[start:end]


def test_sink_failure_health_is_logged_before_it_stops_the_replica() -> None:
    source = APP_SOURCE.read_text()
    timer = source.index("structured_event_drain_timer = TimerEvent")
    failure = source.index("if (!health.healthy)", timer)
    stop = source.index("papp->stop()", failure)
    diagnostic = source.index(
        'log_replica_structured_event_health("sink_failure_pre_stop"',
        failure,
    )

    assert failure < diagnostic < stop
    for field in (
        "first_failure=",
        "last_sequence=",
        "queued_events=",
        "queued_bytes=",
        "complete_records=",
        "dropped_records=",
        "interrupted_tail=",
    ):
        assert field in source


def test_req_and_resp_join_boundaries_are_explicit_on_stderr() -> None:
    source = APP_SOURCE.read_text()
    stop = _function(
        source,
        "void HotStuffApp::stop()",
        "void HotStuffApp::print_stat() const",
    )

    req_begin = stop.index('stage=req_join_begin')
    req_join = stop.index("req_thread.join()")
    req_end = stop.index('stage=req_join_end')
    resp_begin = stop.index('stage=resp_join_begin')
    resp_join = stop.index("resp_thread.join()")
    resp_end = stop.index('stage=resp_join_end')

    assert req_begin < req_join < req_end
    assert resp_begin < resp_join < resp_end
