from xemu_perf_tester import renderer, runner


def render():
    """Run the renderer over result sets."""
    renderer.entrypoint()


def execute():
    """Run the perf tests and generate results."""
    runner.entrypoint()
