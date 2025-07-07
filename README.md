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
