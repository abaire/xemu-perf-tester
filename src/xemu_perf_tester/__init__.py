import sys

from xemu_perf_tester import runner


def execute():
    """Run the perf tests and generate results."""
    sys.exit(runner.entrypoint())
