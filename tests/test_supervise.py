from app import bot


def test_supervise_retries_on_exception_without_dying():
    """A setup/credential error inside one WS attempt must be logged and retried,
    not kill the thread on the first failure."""
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("creds missing")

    bot._supervise(boom, sleep_s=0, _max_iters=3)

    assert len(calls) == 3  # retried every time, never propagated out
