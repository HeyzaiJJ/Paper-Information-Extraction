from __future__ import annotations

import logging
import asyncio
from multiprocessing import get_context
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.logging_setup import (
    bind_log_context,
    configure_main_logging,
    configure_worker_logging,
    get_log_queue,
    reset_log_context,
    shutdown_logging,
)


def _config(tmp_path: Path, max_bytes: int = 1024):
    return SimpleNamespace(
        path=tmp_path / "config" / "runtime.yaml",
        logging={
            "enabled": True,
            "directory": "logs",
            "level": "INFO",
            "max_bytes": max_bytes,
            "backup_count": 2,
            "console": False,
        },
    )


def _child_emit(log_queue):
    configure_worker_logging(log_queue)
    token = bind_log_context(
        request_id="child-request", task_id="child-task", document_id="child-doc",
    )
    try:
        raise RuntimeError("child boom")
    except Exception:
        logging.getLogger("paper.test.worker").exception("child worker failed")
    finally:
        reset_log_context(token)


def test_rotating_files_and_utf8(tmp_path):
    shutdown_logging()
    try:
        configure_main_logging(_config(tmp_path, max_bytes=1024))
        token = bind_log_context(request_id="中文-request", task_id="task-1")
        try:
            logger = logging.getLogger("paper.test")
            for index in range(15):
                logger.info("rotation-%03d %s", index, "x" * 180)
            logger.info("启动日志 中文")
            logging.getLogger("paper.access").info("GET /健康检查 status=200")
        finally:
            reset_log_context(token)
        shutdown_logging()

        log_dir = tmp_path / "logs"
        app_log = log_dir / "app.log"
        access_log = log_dir / "access.log"
        assert app_log.exists()
        assert access_log.exists()
        assert (log_dir / "app.log.1").exists()
        assert "启动日志 中文" in "\n".join(path.read_text(encoding="utf-8") for path in log_dir.glob("app.log*"))
        assert "GET /健康检查" in access_log.read_text(encoding="utf-8")
    finally:
        shutdown_logging()


def test_spawn_worker_traceback_is_persisted(tmp_path):
    shutdown_logging()
    try:
        configure_main_logging(_config(tmp_path))
        ctx = get_context("spawn")
        process = ctx.Process(target=_child_emit, args=(get_log_queue(),))
        process.start()
        process.join(20)
        assert process.exitcode == 0
        shutdown_logging()
        text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
        assert "child worker failed" in text
        assert "Traceback" in text
        assert "RuntimeError: child boom" in text
        assert "request_id=child-request" in text
        assert "task_id=child-task" in text
    finally:
        shutdown_logging()


def test_sensitive_values_are_redacted(tmp_path):
    shutdown_logging()
    try:
        configure_main_logging(_config(tmp_path))
        logging.getLogger("paper.test").error(
            "api_key=secret-value Authorization: Bearer bearer-value"
        )
        shutdown_logging()
        text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
        assert "secret-value" not in text
        assert "bearer-value" not in text
        assert "[REDACTED]" in text
    finally:
        shutdown_logging()


def test_request_id_header_and_access_log(tmp_path):
    import httpx
    import backend.main as marker_platform

    shutdown_logging()

    async def run_request():
        configure_main_logging(_config(tmp_path))
        transport = httpx.ASGITransport(app=marker_platform.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/models", headers={"X-Request-ID": "request-test"}
            )
        shutdown_logging()
        return response

    try:
        response = asyncio.run(run_request())
        assert response.status_code == 200
        assert response.headers["x-request-id"] == "request-test"
        access = (tmp_path / "logs" / "access.log").read_text(encoding="utf-8")
        assert "method=GET path=/api/models status=200" in access
        assert "request_id=request-test" in access
    finally:
        shutdown_logging()


def test_unhandled_request_has_safe_500_and_traceback(tmp_path):
    import httpx
    import backend.main as marker_platform

    async def failing_endpoint():
        raise ValueError("request failure")

    marker_platform.app.add_api_route("/__test_logging_failure", failing_endpoint, methods=["GET"])
    shutdown_logging()

    async def run_request():
        configure_main_logging(_config(tmp_path))
        transport = httpx.ASGITransport(app=marker_platform.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/__test_logging_failure", headers={"X-Request-ID": "error-rid"}
            )
        shutdown_logging()
        return response

    try:
        response = asyncio.run(run_request())
        assert response.status_code == 500
        assert response.headers["x-request-id"] == "error-rid"
        assert response.json()["request_id"] == "error-rid"
        text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
        assert "Traceback" in text
        assert "ValueError: request failure" in text
    finally:
        shutdown_logging()
