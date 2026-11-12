from __future__ import annotations

import httpx

from fprv0.transports.base import BaseTransport


class HTTPJSONTransport(BaseTransport):
    """Client HTTP synchrone pour les appareils qui parlent JSON (ESP32, etc.)."""

    def __init__(self, url: str, timeout: float = 3.0):
        self.url = url
        self.timeout = timeout
        self.client: httpx.Client | None = None

    def open(self) -> None:
        if self.client is None:
            self.client = httpx.Client(timeout=self.timeout)

    def _client(self) -> httpx.Client:
        self.open()
        assert self.client is not None
        return self.client

    def get_json(self) -> dict:
        response = self._client().get(self.url)
        response.raise_for_status()
        return response.json()

    def get_url(self, target_url: str, params: dict | None = None) -> dict:
        """GET sur une URL arbitraire du même appareil (commandes). Tolère une réponse non JSON."""
        response = self._client().get(target_url, params=params)
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            return {"text": response.text[:200]}

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
