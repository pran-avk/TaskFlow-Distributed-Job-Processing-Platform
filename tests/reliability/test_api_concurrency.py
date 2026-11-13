"""Regression: more concurrent requests than database connections must not deadlock.

Each request checks out a connection in its auth dependency and keeps it until the
response is sent, but needs a threadpool thread for every sync step in between. With
more in-flight requests than connections, every thread could end up waiting for a
connection while every connection holder waited for a thread.
"""
import asyncio

import httpx

import main

CONCURRENT_REQUESTS = 150


def test_api_survives_more_concurrent_requests_than_db_connections(client, user):
    async def burst():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60) as api:
            async def submit():
                response = await api.post("/api/v1/jobs", json={"job_type": "simulated_work"}, headers=user["headers"])
                return response.status_code
            return await asyncio.wait_for(
                asyncio.gather(*(submit() for _ in range(CONCURRENT_REQUESTS))), timeout=25
            )

    assert asyncio.run(burst()) == [201] * CONCURRENT_REQUESTS


def test_liveness_answers_while_api_is_saturated(client, user):
    async def probe_during_burst():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60) as api:
            load = [
                asyncio.create_task(api.get("/api/v1/jobs", headers=user["headers"]))
                for _ in range(CONCURRENT_REQUESTS)
            ]
            await asyncio.sleep(0.05)
            live = await asyncio.wait_for(api.get("/api/v1/health/live"), timeout=5)
            await asyncio.gather(*load)
            return live.status_code

    assert asyncio.run(probe_during_burst()) == 200
