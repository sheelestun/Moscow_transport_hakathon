"""HTTP client for the ML service (``ml/src/inference_service.py``)."""

from __future__ import annotations

import httpx


class MlUnavailable(Exception):
    """The ML service couldn't be reached or answered with an error."""


class MlClient:
    def __init__(self, base_url: str, *, timeout_s: float = 10.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(timeout_s, connect=3.0),
                                       transport=transport)

    async def _call(self, method: str, path: str, **kw) -> dict:
        try:
            r = await self._http.request(method, path, **kw)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise MlUnavailable(f"{method} {path}: {type(e).__name__}: {e}") from e

    async def predict_batch(self, requests: list[dict]) -> list[dict]:
        return (await self._call("POST", "/predict/batch", json={"requests": requests}))["responses"]

    async def whatif(self, request: dict) -> dict:
        return await self._call("POST", "/whatif/predict", json=request)

    async def metrics(self) -> dict:
        return await self._call("GET", "/metrics/model")

    async def health(self) -> dict:
        return await self._call("GET", "/health")

    async def aclose(self) -> None:
        await self._http.aclose()
