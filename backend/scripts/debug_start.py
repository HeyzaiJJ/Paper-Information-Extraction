import asyncio
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.runtime_config import RUNTIME_CONFIG, check_remote_inference, prewarm_local_ocr_error, check_local_ocr_error
from backend.knowledge_db import init_database
from backend.temp_assets import cleanup_expired


async def x():
    started = time.time()
    print("remote", flush=True)
    await asyncio.to_thread(check_remote_inference, RUNTIME_CONFIG)
    print("remote done", time.time() - started, flush=True)

    started = time.time()
    print("prewarm", flush=True)
    await asyncio.to_thread(prewarm_local_ocr_error, RUNTIME_CONFIG)
    print("prewarm done", time.time() - started, flush=True)

    started = time.time()
    print("health", flush=True)
    print(await asyncio.to_thread(check_local_ocr_error, RUNTIME_CONFIG), flush=True)
    print("health done", time.time() - started, flush=True)

    started = time.time()
    print("db", flush=True)
    await asyncio.to_thread(init_database)
    print("db done", time.time() - started, flush=True)

    started = time.time()
    print("cleanup", flush=True)
    print(await asyncio.to_thread(cleanup_expired), flush=True)
    print("cleanup done", time.time() - started, flush=True)


asyncio.run(x())
