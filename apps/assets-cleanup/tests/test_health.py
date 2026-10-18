from urllib.request import urlopen

from aap_assets_cleanup.health import HealthState, start_health_server


def test_loopback_health_reports_liveness_separately_from_readiness():
    state = HealthState(poll_seconds=1)
    server = start_health_server(state, "127.0.0.1", 0)
    try:
        with urlopen(
            f"http://127.0.0.1:{server.server_port}/health/live", timeout=2
        ) as response:
            assert response.status == 200
        try:
            urlopen(f"http://127.0.0.1:{server.server_port}/health/ready", timeout=2)
        except OSError as error:
            assert getattr(error, "code", None) == 503
        else:
            raise AssertionError("unready worker returned success")

        state.set_ready(True)
        with urlopen(
            f"http://127.0.0.1:{server.server_port}/health/ready", timeout=2
        ) as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
