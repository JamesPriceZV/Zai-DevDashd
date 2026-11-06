<!-- SPDX-License-Identifier: CC-BY-4.0 -->
# Zai-DevDashd

A local dashboard that turns development records into a readable delivery
outlook: what changed, what is blocked, what is running and what still needs
evidence. It keeps source integration, native validation and distribution as
separate steps. A successful poll cannot turn an old report into a fresh result.

This is the initial public adaptation of a private operations dashboard. Machine
identities, project records, account telemetry, credentials and private history
are excluded. Optional integrations use your own configuration. This public
adaptation is experimental; package validation does not qualify an application
or release you monitor.

## Install and run

Use macOS and Python 3.9 or newer. The base Python package has no third-party
runtime dependencies. Apple hardware, device and local-account features require
the relevant macOS tools; see [dependencies and attributions](THIRD_PARTY_NOTICES.md).

```sh
git clone https://github.com/JamesPriceZV/Zai-DevDashd.git
cd Zai-DevDashd
python3 -m venv .venv
.venv/bin/python -m pip install .
mkdir -p ~/.config/zai-devdashd
cp config.example.toml ~/.config/zai-devdashd/config.toml
chmod 600 ~/.config/zai-devdashd/config.toml
.venv/bin/zai-devdashd --config ~/.config/zai-devdashd/config.toml
```

The default address is `https://127.0.0.1:8780/`. Keep the terminal process
running. The package uses its own state and configuration namespace. Service
installation and restart are optional, explicit actions.

The first run creates a private local certificate. Verify its fingerprint before
trusting it on another device. Never share its private key or use the test
authenticator for a real deployment. A loopback-only run permits the local user
to view the dashboard. Network access requires deliberate binding, certificate
trust and a configured local authentication group.

## Connect your records

Start with the minimal [example configuration](config.example.toml). External
repositories, remote hosts, device discovery, usage records and review telemetry
are disabled until configured. Missing inputs appear as unconnected or unknown.
Provide only records and machines you are authorized to inspect.

The executive feed is an owner report with a configured producer identity and
original observation times. Exact revision and receipt bindings determine which
claims it can support. PR merge counts describe integration activity; they are
not a feature-completion percentage. A complete feature inventory must be
declared before showing a denominator.

Operational completion ranges require comparable successful, receipt-bound
history, a confirmed schedule and satisfied dependencies. Missing history,
blocked work and overruns keep completion unconfirmed. Planning windows are
labeled separately from measured estimates.

Optional Apple Foundation Models interpretations and Sales & Marketing drafts
cite the input evidence. They do not set status, calculate completion times or
grant release approval. Interpretation is disabled by default. Availability
depends on your Mac, Apple Intelligence settings, model assets and execution
context; a failed call leaves the recorded facts available. No paid provider is
substituted automatically.

The Apple CLI path supplies a bundled typed generation schema, preserves closed
stdin and restricts inherited environment settings. Drafts must still pass strict
JSON, citation and claim checks. Schema guidance does not establish factual
accuracy; an invalid response is rejected rather than partially displayed.

## Develop

```sh
.venv/bin/python -m pip install '.[test,build]'
.venv/bin/python -m pytest -q
.venv/bin/python -m build
```

Browser checks additionally need Playwright's separately installed browsers.
Tests use explicitly synthetic records and isolated state. Never substitute real
operational receipts, account exports, credentials or local configuration in a
test fixture intended for publication.

## Licenses and attribution

Code, scripts, configuration examples and original software assets are licensed
under [MIT](LICENSE). Narrative documentation is licensed under
[CC BY 4.0](LICENSE-DOCS.md). Third-party software retains its own terms; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). These licenses do not grant rights
to provider trademarks, model assets or private user data.
