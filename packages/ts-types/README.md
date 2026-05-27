# ts-types

> **Status: placeholder.** This directory is reserved for generated
> TypeScript types from the portal-api OpenAPI spec, used by
> `apps/portal-web` (and any external SDK we eventually publish).
>
> Today `portal-web` re-declares its types inline. The intended workflow
> when this package is populated:
> 1. Export the OpenAPI spec from a running `portal-api`:
>    `curl http://localhost:8085/openapi.json > openapi.json`
> 2. Run `openapi-typescript openapi.json -o packages/ts-types/index.d.ts`
> 3. `apps/portal-web` imports from `@execrelay/ts-types` via the npm
>    workspace alias.
>
> Until that wiring lands, this directory is empty.
