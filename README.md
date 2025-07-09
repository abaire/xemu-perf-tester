xemu-perf-tester
===

Orchestrates running [xemu](xemu.app) benchmarks
using [xemu-perf-tests](https://github.com/abaire/xemu-perf-tests).

# Installation

Install from [Pypi](https://pypi.org/project/xemu-perf-tester/)

```shell
pip install xemu-perf-tester
```

# Use

## Running benchmarks

Run `xemu-perf-run -h` for detailed information on executing the benchmarks.

One time setup: `xemu-perf-run --import-install <path_to_your_xemu.toml_file>`

### Test the latest xemu with the latest benchmark release

The default behavior is to download the latest xemu-perf-tests iso and xemu
release and run the benchmarks using the OpenGL backend. You may pass the
`--use-vulkan` parameter to use Vulkan instead.

```shell
xemu-perf-run
```

### Testing against specific xemu and/or xemu-perf-tests releases

```shell
xemu-perf-run \
  --xemu-tag v0.8.7 \
  --test-tag v12345
```

`--xemu_tag` accepts:

- a xemu release version (e.g., `v0.8.92`)
- the URL of a build action (e.g.,
  `https://github.com/xemu-project/xemu/actions/runs/16152580613`)
- the URL of a pull request (PR) (e.g.,
  `https://github.com/xemu-project/xemu/pull/2329`).

The action and PR options additionally require you to pass a GitHub token using
the `--github-token` argument. See `--help` for details.

### Reusing existing xemu-perf-tests ISO and/or xemu binary

You can use the `--iso` and `--xemu` flags to specify existing artifacts. This
will skip the automated check against the GitHub API for the `latest` tagged
artifacts.

```shell
xemu-perf-run \
  --xemu ~/bin/xemu \
  --iso ~/special_perf_tests.xiso
```

#### Using a development build of xemu on macOS

Some extra flags are needed to utilize a development build of xemu. You will
need to set the `DYLD_FALLBACK_LIBRARY_PATH` environment variable to point at a
valid xemu.app binary and will need to pass the `--no-bundle` argument to
`xemu-perf-run` to prevent it from attempting to find a `xemu.app` bundle
itself.

```shell
DYLD_FALLBACK_LIBRARY_PATH=/path/to/xemu_repo/dist/xemu.app/Contents/Libraries/arm64 \
xemu-perf-run \
  --xemu /path/to/xemu_repo/build/qemu-system-i386 \
  --no-bundle
```

## Test configuration

### Conditional block listing

Tests that cannot be executed on certain versions of xemu may be disallowed
using a `blocklist.json` file specified using the `--block-list-file` command.

The file provides a simple list of JSON objects, each specifying a set of one or
more conditions and one or more test names to be disabled if the
condition is satisfied.

For example, to disable the entire "High vertex count" suite on xemu versions
less than 0.8.54:

```json
{
  "rules": [
    {
      "conditions": [
        "$version < 0.8.54"
      ],
      "skipped": [
        "High vertex count::"
      ]
    }
  ]
}
```

"`conditions`" may use the `$version` variable to test against the runtime
reported version of xemu.

"`skipped`" entries are fully qualified test names, with anything before "::"
referring to a test suite and everything after to a specific test. These are
exact matches, so "Suite::Test" will only disable the test literally named "
Suite::Test" but would still allow "Suite::Test1" to run. The trailing "::" may
be omitted when disallowing an entire test suite. For example "Suite::" is
equivalent to "Suite", both will disable all tests in the "Suite" suite.
