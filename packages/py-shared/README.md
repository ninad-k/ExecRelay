# py-shared

> **Status: placeholder.** This directory is reserved for shared Python
> code (Pydantic models, asyncpg helpers, RBAC checks, report payload
> schemas) that today lives inline in `apps/portal-api/app.py` and the
> other cold-path services. As those files grow, common code should be
> extracted here and imported via `pip install -e packages/py-shared`.
>
> If you're starting that extraction, the natural first candidates are:
> - The bearer-token / `current_user` dependency (currently in
>   `apps/portal-api/app.py`)
> - The asyncpg connection-pool lifespan helper
> - The RBAC role-check decorators
> - The protobuf wire-format encoder/decoder helpers
>   (`_pb_varint`, `_pb_str_field`, `encode_signal_proto`,
>   `decode_signal_proto` in `apps/portal-api/app.py`)
>
> Until that extraction lands, this directory is empty.
