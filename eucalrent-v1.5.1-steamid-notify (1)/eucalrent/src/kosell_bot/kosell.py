from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class KosellError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Balance:
    rub: float
    usd: float
    username: str
    role: str


class KosellClient:
    def __init__(self, api_key: str, base_url: str, timeout: float = 30.0):
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "X-API-Key": api_key,
                "Accept": "application/json",
                "User-Agent": "eucalrent/1.1",
            },
            timeout=timeout,
            trust_env=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        attempts: int = 3,
    ) -> Any:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                response = await self._client.request(
                    method, path, json=json, params=params, headers=headers
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise KosellError(
                    "KOSell временно недоступен по сети",
                    retryable=True,
                ) from exc

            if response.status_code < 400:
                if not response.content:
                    return {}
                try:
                    return response.json()
                except ValueError as exc:
                    raise KosellError(
                        "KOSell вернул некорректный JSON",
                        status_code=response.status_code,
                        retryable=response.status_code >= 500,
                    ) from exc

            payload: dict[str, Any]
            try:
                raw = response.json()
                payload = raw if isinstance(raw, dict) else {}
            except ValueError:
                payload = {}

            error_code = str(payload.get("error") or "") or None
            message = str(
                payload.get("message")
                or payload.get("detail")
                or f"KOSell HTTP {response.status_code}"
            )
            retryable = response.status_code == 429 or response.status_code >= 500

            if retryable and attempt + 1 < attempts:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = min(float(retry_after), 10.0) if retry_after else 1.5 * (attempt + 1)
                except ValueError:
                    delay = 1.5 * (attempt + 1)
                await asyncio.sleep(delay)
                continue

            raise KosellError(
                message,
                status_code=response.status_code,
                error_code=error_code,
                retryable=retryable,
            )

        raise KosellError(str(last_error or "Неизвестная ошибка KOSell"), retryable=True)

    async def get_balance(self) -> Balance:
        data = await self._request("GET", "/account/balance")
        return Balance(
            rub=float(data["balance_rub"]),
            usd=float(data["balance_usd"]),
            username=str(data["username"]),
            role=str(data["role"]),
        )

    async def list_products(self, search: str | None = None) -> list[dict[str, Any]]:
        params = {"currency": "RUB"}
        if search:
            params["search"] = search
        data = await self._request("GET", "/rental/products", params=params)
        if not isinstance(data, list):
            raise KosellError("KOSell вернул некорректный список игр")
        return [item for item in data if isinstance(item, dict)]

    async def calculate_price(self, product_id: int, hours: int) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/rental/calculate-price",
            json={"product_id": product_id, "hours": hours},
        )

    async def rent(
        self,
        product_id: int,
        duration_hours: int,
        currency: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/rental/rent",
            json={
                "product_id": product_id,
                "duration_hours": duration_hours,
                "currency": currency,
                "idempotency_key": idempotency_key,
            },
            idempotency_key=idempotency_key,
        )

    async def get_credentials(self, rental_uid: str) -> dict[str, str]:
        data = await self._request(
            "GET", f"/rental/{rental_uid}/credentials"
        )
        return {
            "steam_login": str(data["steam_login"]),
            "steam_password": str(data["steam_password"]),
        }

    async def get_code(self, rental_uid: str) -> dict[str, Any]:
        return await self._request("GET", f"/rental/{rental_uid}/code")

    async def terminate(self, rental_uid: str) -> dict[str, Any]:
        data = await self._request(
            "POST", f"/rental/{rental_uid}/terminate", attempts=3
        )
        return data if isinstance(data, dict) else {"result": data}
