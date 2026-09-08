from __future__ import annotations

import asyncio
from typing import Any

import httpx


class SteamSmmError(RuntimeError):
    pass


class SteamSmmClient:
    def __init__(self, api_token: str, base_url: str = "https://steamsmm.ru/api", timeout: float = 30.0):
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "eucalrent/1.3",
            },
            timeout=timeout,
            trust_env=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(3):
            try:
                response = await self._client.request(method, path, json=json)
                if response.status_code < 400:
                    raw = response.json() if response.content else {}
                    return raw if isinstance(raw, dict) else {}
                try:
                    raw = response.json()
                except ValueError:
                    raw = {}
                message = str(raw.get("message") or raw.get("detail") or f"SteamSMM HTTP {response.status_code}")
                if response.status_code in {429, 500, 502, 503} and attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise SteamSmmError(message)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last = exc
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise SteamSmmError("SteamSMM временно недоступен по сети") from exc
        raise SteamSmmError(str(last or "Неизвестная ошибка SteamSMM"))

    async def list_autoreg_products(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/autoreg/products")
        data = payload.get("data") if isinstance(payload, dict) else None
        groups = data.get("groups") if isinstance(data, dict) else None
        result: list[dict[str, Any]] = []
        if isinstance(groups, dict):
            for group_items in groups.values():
                if isinstance(group_items, dict):
                    group_items = group_items.get("items", [])
                if not isinstance(group_items, list):
                    continue
                for item in group_items:
                    if isinstance(item, dict):
                        result.append(item)
        return result

    async def commend_price(self, friendly: int, teacher: int, leader: int) -> dict[str, Any]:
        return await self._request(
            "POST", "/commend/price",
            json={"friendly": friendly, "teacher": teacher, "leader": leader},
        )

    async def autoreg_price(self, category_id: int, quantity: int) -> dict[str, Any]:
        return await self._request(
            "POST", "/autoreg/price",
            json={"category_id": category_id, "quantity": quantity},
        )

    async def autoreg_create(self, category_id: int, quantity: int) -> dict[str, Any]:
        return await self._request(
            "POST", "/autoreg/create",
            json={"category_id": category_id, "quantity": quantity},
        )

    async def commend_check(self, target: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/commend/check", json={"target": target}
        )

    async def commend_create(self, target: str, friendly: int, teacher: int, leader: int) -> dict[str, Any]:
        return await self._request(
            "POST", "/commend/create",
            json={
                "target": target,
                "friendly": friendly,
                "teacher": teacher,
                "leader": leader,
            },
        )

    async def order_status(self, order_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/order/status/{order_id}")
