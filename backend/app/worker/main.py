import asyncio
import logging

import httpx
from prometheus_client import start_http_server

from app.observability import tracing
from app.worker.client import HttpWorkerClient
from app.worker.config import worker_settings
from app.worker.loop import run_forever

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("worker")


async def main() -> None:
    logger.info(
        "starting worker: control_plane=%s poll_interval=%ss",
        worker_settings.control_plane_base_url,
        worker_settings.poll_interval_seconds,
    )
    if worker_settings.metrics_port:
        start_http_server(worker_settings.metrics_port)
        logger.info("metrics on :%s/metrics", worker_settings.metrics_port)
    async with httpx.AsyncClient() as http_client:
        tracing.instrument_httpx(http_client, tracing.configured_provider("worker"))
        client = HttpWorkerClient(
            http_client,
            worker_settings.control_plane_base_url,
            worker_settings.request_timeout_seconds,
        )
        await run_forever(
            client,
            worker_settings.poll_interval_seconds,
            worker_settings.default_evaluation_window_seconds,
        )


if __name__ == "__main__":
    asyncio.run(main())
