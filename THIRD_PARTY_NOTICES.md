# Third-Party Notices

The generated file `assets/parallel-indicator-host.bundle.js` contains code
from the following packages. They are build inputs for the local MCP App
indicator and are not Python runtime dependencies.

## MCP Ext Apps

- Package: `@modelcontextprotocol/ext-apps` 1.7.5
- Repository: <https://github.com/modelcontextprotocol/ext-apps>
- License: transitional Apache-2.0/MIT; upstream documentation is CC-BY-4.0
- Included license: `third_party/licenses/ext-apps-1.7.5-LICENSE.txt`

## Model Context Protocol SDK

- Package: `@modelcontextprotocol/sdk` 1.30.0
- Repository: <https://github.com/modelcontextprotocol/typescript-sdk>
- License: MIT
- Included license: `third_party/licenses/mcp-sdk-1.30.0-LICENSE.txt`

## Zod

- Package: `zod` 4.4.3
- Repository: <https://github.com/colinhacks/zod>
- License: MIT
- Included license: `third_party/licenses/zod-4.4.3-LICENSE.txt`

## Zod to JSON Schema

- Package: `zod-to-json-schema` 3.25.2
- Repository: <https://github.com/StefanTerdell/zod-to-json-schema>
- License: ISC
- Included license: `third_party/licenses/zod-to-json-schema-3.25.2-LICENSE.txt`

## esbuild

- Package: `esbuild` 0.28.2
- Repository: <https://github.com/evanw/esbuild>
- License: MIT
- Use: build tool only; it is not included in the generated browser bundle.

Exact package integrity hashes and transitive build inputs are recorded in
`package-lock.json`; the reviewed bundle closure and license-file mappings have
one machine-readable source of truth in
`third_party/bundle-dependencies.json`. MPL-2.0 applies to current original
project code, while releases through `0.13.0` retain their historical MIT
grant; third-party components remain under their respective licenses. The UI
uses the normal `@modelcontextprotocol/ext-apps` entrypoint so
its SDK, Zod, and Zod-to-JSON-Schema versions are explicit package-lock inputs;
it does not ship the dependency-vendored `app-with-deps` entrypoint. The build
fails if the emitted metafile package closure changes. Complete upstream
license texts shipped with the browser bundle are retained under
`third_party/licenses/` even though minification removes inline legal comments.
