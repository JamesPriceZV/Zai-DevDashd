# Dependencies and attributions

Zai-DevDashd's first-party code is MIT licensed. Its narrative documentation is
CC BY 4.0. No third-party Python, JavaScript, browser, Apple framework or model
binaries are vendored in the source distribution. The SVG identity artwork is
original source artwork supplied with this project.

## Python packages

The base application declares no third-party Python runtime dependencies. It
uses Python's standard library. Python itself is distributed under the
[Python Software Foundation license and associated notices](https://docs.python.org/3/license.html).

The following are separately installed build or development tools. Their
transitive dependencies retain the license notices supplied by their publishers.

| Component | Use | Upstream terms |
| --- | --- | --- |
| [setuptools](https://github.com/pypa/setuptools) | Build backend | [MIT](https://github.com/pypa/setuptools/blob/main/LICENSE) |
| [build](https://github.com/pypa/build) | Optional wheel/source build frontend | [MIT](https://github.com/pypa/build/blob/main/LICENSE) |
| [pytest](https://github.com/pytest-dev/pytest) | Optional tests | [MIT](https://github.com/pytest-dev/pytest/blob/main/LICENSE) |
| [Playwright for Python](https://github.com/microsoft/playwright-python) | Optional browser tests | [Apache 2.0](https://github.com/microsoft/playwright-python/blob/main/LICENSE) |

Playwright's browser downloads have additional upstream components and notices.
They are not included in Zai-DevDashd. Dependency ranges are declared in
`pyproject.toml`; installation resolves a compatible version for your Python.

## Optional external integrations

These tools and services are not bundled or sublicensed by this repository.
Install and configure only the integrations you need, under their own terms.

| Tool or platform | Integration |
| --- | --- |
| macOS utilities | Local system readings, TLS setup and local-account authentication |
| OpenSSH | Explicitly configured read-only remote metadata |
| Xcode command-line tools | Optional Apple device metadata; no build or release dispatch |
| [GitHub CLI](https://cli.github.com/) | Read-only repository activity using your authentication |
| [CodexBar](https://github.com/steipete/CodexBar) | Optional reported usage metadata; [upstream MIT license](https://github.com/steipete/CodexBar/blob/main/LICENSE) |
| Apple Foundation Models and `fm` | Optional Apple model interpretation, subject to Apple's availability and terms |

The optional native helper imports Apple's Foundation and FoundationModels
frameworks and declares no third-party Swift package dependencies. Apple model
assets and frameworks must be obtained through supported Apple mechanisms.

Provider names and documentation links identify integrations and factual
sources. They do not imply sponsorship or endorsement. Published hardware
specifications and provider documentation remain attributed in the relevant
source comments; a specification is distinct from a measured device reading.

## Adapted CodexBar source

`src/zai_devdashd/cbcli.py::linear_pace` adapts the weekly pace and workday
calculation from CodexBar's `Sources/CodexBarCore/UsagePace.swift`. It calculates
expected use and a conditional exhaustion time; it is not a promise of future
availability. The Python adaptation does not claim full API or behavioral parity.

The primary upstream source and license were checked at commit
[`02b5a6364049df0dfa6299317f676eb3ed69c003`](https://github.com/steipete/CodexBar/tree/02b5a6364049df0dfa6299317f676eb3ed69c003).
The source Git blob is `e0a01936ba4b85452534053b89682f6a599e569e`, and the
license Git blob is `0ae0cb57d8c6c1417f796b39fc0d6a7f2f7c5c39`. This identifies
the version verified for this distribution; the earlier adaptation's exact
upstream revision was not recorded.

Copyright (c) 2026 Peter Steinberger. The complete upstream MIT copyright and
permission notice is retained in `LICENSES/CodexBar-MIT.txt` and distributed
with the source and wheel. Those upstream terms continue to apply to the
adapted source; the documentation license does not replace them.
