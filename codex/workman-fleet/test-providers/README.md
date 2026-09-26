# Declarative Workman test providers

Register reviewed Markdown skills/plugin adapters or a resource reference to an
already configured MCP server. This registry does not install arbitrary code,
launch MCP servers, add credentials, make network calls, or change doctrine.

One provider is selected per explicit test ID. Default is none; `off` clears
that test's selection. Normal sessions and other tests are unaffected.

A local skill/plugin manifest uses schema_version 1, id, kind (skill or plugin),
scope workman_test, enabled_by_default false, package-relative Markdown path,
SHA-256, immutable source revision, source reference, version and allowed levels.
Copy only reviewed Markdown into this package, preserving its license. An MCP
manifest replaces path with server and resource_uri; its server must already be
configured and its returned resource must match the reviewed SHA-256. Registration
only stores the reference. The host decides whether to read it under existing
permissions. Model calls, executable tools, credentials and new services are not
part of this test interface.

Use fleet_test_mode actions list, register, select, get, payload and off. Select
requires a nonsecret test_id and provider; payload returns unmodified pinned
Markdown or the declared MCP resource handoff. No automatic source updates.
Raw Caveman is pinned under integrations/caveman and loaded only on request.
