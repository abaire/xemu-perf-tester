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

For PRs and actions, the build artifact for your platform is taken from the
newest successful run of the xemu `CI` workflow (override with `--ci-workflow`).
The tool exits with an error if there is no such run (e.g., the build is still
in progress, failed, or its artifacts have expired); it never falls back to a
release build.

Downloading artifacts requires an authenticated GitHub token, chosen in this
order:

1. `--github-token`, if given.
1. The [GitHub CLI](https://cli.github.com/)'s token (`gh auth token`), if `gh`
   is installed and logged in.
1. The same token used to submit results (you will be prompted to authorize it
   in a browser the first time).

Results produced from a PR or action build include a `dev_build` section
(PR number, head SHA, workflow run, base xemu version) and are ingested
separately from release results.

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

### Pinned xemu settings

Every run writes its own `xemu.toml`, so results don't depend on how xemu is
set up on the host. Besides the BIOS, boot ROM and HDD paths, it sets:

* `display.renderer`: OpenGL, or Vulkan with `--use-vulkan` / `--renderers VK`
* `display.quality.surface_scale = 1`
* `display.window`: windowed (`fullscreen_on_startup = false`),
  `startup_size = '1280x960'`, and `vsync` on unless `--vsync off` is given
* `display.ui`: `show_menubar = false`, `show_notifications = false`,
  `fit = 'scale'`
* `perf.cache_shaders = false`, so shaders are compiled the same way on every
  launch

### Sustained runs and the contention matrix

`--run-mode sustained` makes the tester run each test for a stretch of 60 FPS
frames instead of profiling a single frame (see the xemu-perf-tests README).
Each test runs at the load calibrated on real hardware, which the ISO reads from
its `calibration.json`.

`--matrix contention` runs the `PfifoContention` suite in sustained mode with
each renderer, with vsync both on and off, launching xemu fresh 3 times for
each combination:

```shell
xemu-perf-run --matrix contention
```

Explicit flags override the preset:

* `--renderers GL VK`: the renderers to run with.
* `--vsync on off`: the vsync settings to run with.
* `--repeat N`: the number of separate launches for each renderer and vsync
  setting. Defaults to 3 in sustained mode and 1 in single mode.
* `--just-suites`: the suites to run.
* `--warmup-frames`, `--measure-frames`: override the number of frames per test,
  for example for a quick check. `--measure-frames` must be at least 1.
* `--timeout-seconds`: the longest a launch may run before it is killed.
  Defaults to 1800 in sustained mode. A launch that is killed before the tests
  finish is recorded as failed.

A sustained run writes a single file,
`results/<xemu version>/<machine>-sustained-<UTC time>.json`. It contains every
launch's results under `cells`, with the xemu settings, xemu's GPU information
and the host conditions (see below) of each launch, and the hardware
measurements from the ISO's `calibration.json` under `hardware_baseline`. The
file is rewritten after every launch, so a run that is stopped keeps the
launches that finished.

If xemu reports that it can't use the requested renderer (for example, a build
without Vulkan support) and switches to another one, that launch is recorded as
failed and the renderer's remaining launches are skipped.

Only standard results can be uploaded: `--upload-results` is rejected in
sustained mode, with `--vsync off`, with `--repeat` greater than 1, and with
frame overrides.

### Comparing results

`xemu-perf-compare` reports a sustained results file against the hardware and,
optionally, against the results of another xemu build:

```shell
xemu-perf-compare candidate.json baseline.json > report.md
```

For each renderer and vsync setting, and each test, it shows:

* the number of repeats that produced results, the busy time p50 and p95, each
  the median over those repeats, and the spread (max - min) of p95 across them;
* `vs HW`: busy p95 divided by the hardware's busy p95. 1.00 or less means
  xemu is at least as fast as the hardware;
* with a baseline, the change in p95. A change is marked significant only when
  it is larger than the spreads of both files combined, which needs at least 2
  repeats in each;
* missed vblanks, totalled over the repeats, and notes about fence timeouts,
  aborted runs, loads that differ from the hardware's or the baseline's, and
  tests that only one of the files has results for.

Each renderer and vsync setting also notes how many launches failed, and the
report warns when the files come from different machines or ISOs. Only
sustained results files can be compared.

`--format json` writes the same data as JSON, and `--output` writes to a file.

### Reducing host noise

Busy times are affected by anything else the host is doing. For results that
can be compared:

* close other applications, especially ones that use the GPU (browsers, video);
* keep laptops on mains power and disable power saving modes;
* on Linux, use the `performance` CPU governor;
* keep the xemu window visible and unobstructed on the main display;
* run baseline and candidate builds back to back on the same machine.

The results record the load average, whether the machine is on mains power and
(on Linux) the CPU governor, sampled before each launch, alongside the display
refresh rate.

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
