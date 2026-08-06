"""`SupabaseStorage` — the one place this codebase uploads bytes to Supabase Storage.

Deliberately shaped like `app.infrastructure.db.pool` and `app.infrastructure.queue.pool`:
a pure settings-to-client factory, plus a thin wrapper around the one operation this feature
needs. Storage is a REST API over HTTPS (`{SUPABASE_URL}/storage/v1/object/...`), not a
client library with its own connection pool to manage, so "the client" here is an ordinary
`httpx.AsyncClient` rather than anything Supabase-specific.

**Deliberately no `open_/get_/close_` process-wide singleton — see the package docstring
(`__init__.py`) for why.** In short: `db/pool.py` and `queue/pool.py` need one because
FastAPI's request path reaches for a pool built once at API startup through a dependency.
Nothing under `app/api/` calls Storage; only `app.features.crawl.service.CrawlService`
does, and it is built fresh per arq job from resources already sitting on that job's `ctx`
(`app/worker/settings.py`'s `open_worker_resources`). A module singleton here would be a
second place to remember to close, guarding a lifetime `ctx` already manages.

**`StorageUploadError` messages carry a status code and the bucket-qualified object path,
and nothing else — never the service key, never the request URL, never the response body**
(ARCHITECTURE.md §9.4, CLAUDE.md #1). `SUPABASE_URL` is part of every request URL this module
builds, so the URL itself is treated as sensitive by omission rather than trusted to be safe
on inspection; a response body from an unauthenticated or misconfigured endpoint is exactly
the kind of thing that could echo back a header or a fragment of the request, so it is never
interpolated into an exception either. `tests/test_storage_client.py` asserts the service key
appears in neither `str(exc)` nor `repr(exc)` for both failure paths below.
"""

from urllib.parse import quote

import httpx

from app.core.settings import Settings


class StorageUploadError(Exception):
    """Raised by `SupabaseStorage.upload` on any non-2xx response or `httpx.HTTPError`.

    See the module docstring for what this exception's message may and may not contain.
    """


class SupabaseStorage:
    """A thin wrapper around Supabase Storage's REST upload endpoint.

    Holds an already-built `httpx.AsyncClient` but never closes it — the same "handed a
    client, never owns it" relationship `RunService`/`CrawlService` have to the asyncpg pool
    they are constructed with. Whatever built the client (`build_storage_client` below, or a
    test) is responsible for closing it; see `app/worker/settings.py`'s
    `close_worker_resources`, which closes `ctx["storage_client"]` for exactly this reason.
    """

    def __init__(
        self, client: httpx.AsyncClient, *, base_url: str, service_key: str, bucket: str
    ) -> None:
        self._client = client
        self._base_url = base_url
        self._service_key = service_key
        self._bucket = bucket

    @property
    def bucket(self) -> str:
        """The bucket every `upload()` call writes into. Exposed so a caller building an
        object's bucket-qualified path up front (or asserting one in a test) does not need
        a second reference to `Settings.supabase_storage_bucket`."""
        return self._bucket

    async def upload(self, object_path: str, data: bytes, *, content_type: str) -> str:
        """Upload `data` to `{bucket}/{object_path}` and return that bucket-qualified path.

        Args:
            object_path: The path within the bucket, e.g. what
                `app.features.crawl.internals.payload.payload_object_path` builds. Percent-
                encoded with `urllib.parse.quote(..., safe="/")` before it reaches the URL,
                so a path segment stays a single path segment on the wire while `/` keeps
                separating them — this project's object paths are UUIDs and a fixed suffix,
                which need no encoding in practice, but a caller should not have to know that
                to call this safely.
            data: The bytes to upload, already in their final on-the-wire form (gzip-
                compressed, for the crawl payload). This method does not transform them.
            content_type: The `Content-Type` header to send. Never inferred from `data` or
                `object_path` — the caller (`app.features.crawl.internals.payload`) is the
                one place that knows what it produced.

        Returns:
            The bucket-qualified path, `f"{bucket}/{object_path}"` — not the bare
            `object_path`. That is exactly the shape the ticket specifies for
            `runs.storage_path`, and it makes the stored value self-describing even though
            the bucket name is configurable (`Settings.supabase_storage_bucket`): a reader
            of the column never has to consult settings to know which bucket a given row's
            object lives in.

        Raises:
            StorageUploadError: on any non-2xx response, or if the request itself failed
                (`httpx.HTTPError` — a connection error, a timeout, a TLS failure). The
                original exception is chained with `raise ... from exc` in the latter case,
                so a caller logging with `exc_info=True` still gets the real cause; the
                message on `StorageUploadError` itself never does (see the module
                docstring).
        """
        bucket_qualified_path = f"{self._bucket}/{object_path}"
        url = (
            f"{self._base_url.rstrip('/')}/storage/v1/object/"
            f"{self._bucket}/{quote(object_path, safe='/')}"
        )
        headers = {
            "Authorization": f"Bearer {self._service_key}",
            "apikey": self._service_key,
            "Content-Type": content_type,
            # A `run_id` is unique, so the object at this path should never already exist —
            # but arq can redeliver a job (`WorkerSettings.MAX_JOBS = 2` makes concurrent
            # delivery a real scenario, the same fact `RunsWriter.claim_pending`'s docstring
            # documents for the database side of this same redelivery). Without `x-upsert`,
            # a redelivered job's second upload attempt gets a 409 from Storage and fails an
            # otherwise-healthy run; with it, re-uploading the same bytes to the same path is
            # a no-op success instead.
            "x-upsert": "true",
        }

        try:
            response = await self._client.post(url, content=data, headers=headers)
        except httpx.HTTPError as exc:
            raise StorageUploadError(
                f"Could not reach Supabase Storage to upload {bucket_qualified_path!r} "
                f"({type(exc).__name__})"
            ) from exc

        if not response.is_success:
            # Never the response body: an unauthenticated or misconfigured endpoint could
            # echo back something sensitive, and this exception's contract is "status code
            # and path, nothing else" (module docstring).
            raise StorageUploadError(
                f"Supabase Storage returned {response.status_code} for upload of "
                f"{bucket_qualified_path!r}"
            )

        return bucket_qualified_path


def build_storage_client(settings: Settings) -> httpx.AsyncClient:
    """Build the dedicated `httpx.AsyncClient` Storage uploads are issued through.

    Deliberately NOT `app.features.crawl.http_client.build_crawl_client`'s client, reused.
    That client's 10-second timeout (`Settings.crawl_request_timeout_s`) is tuned for
    fetching one page during a crawl, not for pushing a compressed, potentially multi-
    megabyte payload to Storage — `Settings.storage_upload_timeout_s` (120s default) is a
    separate, larger budget for exactly that reason. Its `follow_redirects=False` also exists
    for an SSRF reason (`internals/ssrf.py`'s check-then-connect defense) that has nothing to
    do with talking to Supabase's own API, so there is no shared configuration to reuse
    beyond "it's an `httpx.AsyncClient`."

    Opens no socket and makes no network call — constructing an `httpx.AsyncClient` never
    does — so `app/worker/settings.py`'s `open_worker_resources` can call this unconditionally
    the same way it already does for `build_crawl_client`.
    """
    return httpx.AsyncClient(timeout=httpx.Timeout(settings.storage_upload_timeout_s))


def build_supabase_storage(client: httpx.AsyncClient, settings: Settings) -> SupabaseStorage:
    """Build the `SupabaseStorage` client the worker uploads crawl payloads through.

    Takes `client` rather than building one itself, mirroring `build_storage_client`'s
    separation from this function: `app/worker/settings.py`'s `open_worker_resources` builds
    the client once and publishes it on `ctx["storage_client"]` so `close_worker_resources`
    has something to close, and hands the same client here to build the thing that actually
    uses it — the same "who builds it, closes it" split `SupabaseStorage` itself documents.
    """
    return SupabaseStorage(
        client,
        base_url=settings.supabase_url,
        service_key=settings.supabase_secret_key,
        bucket=settings.supabase_storage_bucket,
    )
