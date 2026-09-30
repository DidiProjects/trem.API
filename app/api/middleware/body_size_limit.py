from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodySizeLimitMiddleware:
    """
    The app is the single authority on upload size: the reverse proxy in front
    of it is configured without a body limit, so every request reaches here and
    gets a JSON 413 instead of the proxy's HTML page.
    """

    def __init__(
        self,
        app: ASGIApp,
        default_limit: int,
        limits_by_prefix: dict[str, int] | None = None,
    ) -> None:
        self.app = app
        self.default_limit = default_limit
        self.limits_by_prefix = sorted(
            (limits_by_prefix or {}).items(), key=lambda item: len(item[0]), reverse=True
        )

    def limit_for(self, path: str) -> int:
        for prefix, limit in self.limits_by_prefix:
            if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
                return limit
        return self.default_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.limit_for(scope["path"])
        too_large = JSONResponse(
            {"detail": f"Request body exceeds the {limit // (1024 * 1024)} MB limit"},
            status_code=413,
        )

        content_length = dict(scope["headers"]).get(b"content-length")
        if content_length is not None and content_length.isdigit() and int(content_length) > limit:
            await too_large(scope, receive, send)
            return

        received = 0
        response_started = False
        rejected = False

        # Chunked uploads carry no Content-Length, so the running total is the
        # only guard. Raising from here does not work: Starlette parses multipart
        # inside a TaskGroup, the error arrives at FastAPI wrapped in an
        # ExceptionGroup and is turned into a 400. So the 413 is sent from here,
        # the app sees a disconnect, and whatever it answers afterwards is dropped.
        async def limited_receive() -> Message:
            nonlocal received, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit and not response_started:
                    rejected = True
                    await too_large(scope, receive, send)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if rejected:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not rejected:
                raise
