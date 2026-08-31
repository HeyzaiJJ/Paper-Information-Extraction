from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
import time

import httpx
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import backend.main as marker_platform
import backend.temp_assets as temp_assets


class FakeProcess:
    def __init__(self):
        self.alive = True
        self.terminated = 0
        self.killed = 0

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated += 1
        self.alive = False

    def kill(self):
        self.killed += 1
        self.alive = False

    def join(self, _timeout=None):
        return None


class PopenProcess:
    """Small multiprocessing.Process-compatible adapter for real tree tests."""

    def __init__(self, popen: subprocess.Popen):
        self.popen = popen
        self.pid = popen.pid

    def is_alive(self):
        return self.popen.poll() is None

    def terminate(self):
        self.popen.terminate()

    def kill(self):
        self.popen.kill()

    def join(self, timeout=None):
        try:
            self.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass


class SpawnableFakeProcess(FakeProcess):
    def __init__(self):
        super().__init__()
        self.alive = False
        self.started = 0

    @property
    def pid(self):
        return None

    def start(self):
        self.started += 1
        self.alive = True


class UnkillableFakeProcess(FakeProcess):
    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1


def _wait_for_file(path: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return int(path.read_text(encoding="utf-8").strip())
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for child PID file: {path}")


def _start_process_tree(pid_file: Path) -> PopenProcess:
    script = (
        "import subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8'); time.sleep(60)"
    )
    return PopenProcess(subprocess.Popen([sys.executable, "-c", script, str(pid_file)]))


def _force_cleanup(process: PopenProcess | None):
    if process is None or not process.is_alive():
        return
    try:
        root = psutil.Process(process.pid)
        targets = root.children(recursive=True) + [root]
        for target in targets:
            try:
                target.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(targets, timeout=3)
    except psutil.NoSuchProcess:
        pass


def _reset_registries():
    marker_platform.PAPER_PREP_TASKS.clear()
    marker_platform.MATERIAL_TASKS.clear()
    marker_platform.CANCELLED_PAPER_PREP_IDS.clear()
    marker_platform.DELETED_WORKSPACE_DOCUMENTS.clear()


def test_cancel_before_prepare_registration_is_remembered():
    _reset_registries()

    result = asyncio.run(marker_platform._cancel_paper_prepare_task("prep_early_cancel"))

    assert result == {
        "ok": True,
        "pending": True,
        "terminated_pids": [],
        "remaining_pids": [],
    }
    assert "prep_early_cancel" in marker_platform.CANCELLED_PAPER_PREP_IDS
    assert "prep_new_run" not in marker_platform.CANCELLED_PAPER_PREP_IDS


def test_cancelled_upload_id_never_starts_prepare_process():
    _reset_registries()
    marker_platform.CANCELLED_PAPER_PREP_IDS["prep_cancelled_upload"] = time.time()

    async def run_request():
        transport = httpx.ASGITransport(app=marker_platform.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/api/prepare_paper",
                data={
                    "prepare_task_id": "prep_cancelled_upload",
                    "client_document_id": "cf_cancelled_upload",
                },
                files={"file": ("paper.pdf", b"%PDF-1.4", "application/pdf")},
            )

    response = asyncio.run(run_request())

    assert response.status_code == 409
    assert response.json()["cancelled"] is True
    assert "prep_cancelled_upload" not in marker_platform.PAPER_PREP_TASKS


def test_cancel_active_prepare_terminates_process_and_finishes_task():
    _reset_registries()
    process = FakeProcess()
    marker_platform.PAPER_PREP_TASKS["prep_active"] = {
        "task_id": "prep_active",
        "created": 0,
        "done": False,
        "success": False,
        "result": {},
        "proc": process,
        "document_id": "missing-staging-document",
    }

    result = asyncio.run(marker_platform._cancel_paper_prepare_task("prep_active"))
    task = marker_platform.PAPER_PREP_TASKS["prep_active"]

    assert result["cancelled"] is True
    assert process.terminated == 1
    assert task["done"] is True
    assert task["cancelled"] is True
    assert task["success"] is False
    assert task["result"] == {}


def test_workspace_delete_stops_all_tasks_for_document():
    _reset_registries()
    prep_process = FakeProcess()
    material_processes = [FakeProcess(), FakeProcess()]
    marker_platform.PAPER_PREP_TASKS["prep_delete"] = {
        "task_id": "prep_delete",
        "created": 0,
        "done": False,
        "success": False,
        "result": {},
        "proc": prep_process,
        "document_id": "missing-delete-document",
        "client_document_id": "cf_delete_me",
    }
    for index, process in enumerate(material_processes):
        marker_platform.MATERIAL_TASKS[f"material_{index}"] = {
            "task_id": f"material_{index}",
            "created": 0,
            "done": False,
            "success": False,
            "proc": process,
            "document_ids": ["cf_delete_me"],
        }

    async def run_delete():
        transport = httpx.ASGITransport(app=marker_platform.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(
                "DELETE",
                "/api/material_documents/cf_delete_me",
                json={
                    "prepare_task_id": "prep_delete",
                    "server_document_id": "missing-delete-document",
                },
            )

    response = asyncio.run(run_delete())

    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert response.json()["remaining_pids"] == []
    assert sorted(response.json()["terminated_task_ids"]) == [
        "material_0", "material_1", "prep_delete",
    ]
    assert prep_process.terminated == 1
    assert all(process.terminated == 1 for process in material_processes)
    assert marker_platform.PAPER_PREP_TASKS == {}
    assert marker_platform.MATERIAL_TASKS == {}
    assert "cf_delete_me" in marker_platform.DELETED_WORKSPACE_DOCUMENTS


def test_deleted_document_cannot_start_late_material_task():
    _reset_registries()
    marker_platform.DELETED_WORKSPACE_DOCUMENTS["cf_deleted"] = 10**20

    async def run_request():
        transport = httpx.ASGITransport(app=marker_platform.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/api/material_extract",
                json={
                    "papers": [{"id": "cf_deleted", "document_id": "missing"}],
                    "parts": ["part1"],
                },
            )

    response = asyncio.run(run_request())

    assert response.status_code == 410
    assert response.json()["deleted"] is True
    assert marker_platform.MATERIAL_TASKS == {}


def test_delete_failure_keeps_task_registered_for_retry():
    _reset_registries()
    process = UnkillableFakeProcess()
    marker_platform.MATERIAL_TASKS["material_stuck"] = {
        "task_id": "material_stuck",
        "created": time.time(),
        "done": False,
        "success": False,
        "proc": process,
        "document_ids": ["cf_stuck"],
    }

    async def run_delete():
        transport = httpx.ASGITransport(app=marker_platform.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.delete("/api/material_documents/cf_stuck")

    response = asyncio.run(run_delete())

    assert response.status_code == 500
    assert response.json()["deleted"] is False
    assert "material_stuck" in marker_platform.MATERIAL_TASKS
    assert process.is_alive() is True
    process.alive = False
    _reset_registries()


def test_delete_document_dir_is_idempotent_and_scoped(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    document = staging / "task-one" / "doc-one"
    sibling = staging / "task-two" / "doc-two"
    document.mkdir(parents=True)
    sibling.mkdir(parents=True)
    (document / "asset.txt").write_text("delete", encoding="utf-8")
    (sibling / "asset.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(temp_assets, "STAGING_DIR", staging)

    assert temp_assets.delete_document_dir("doc-one") is True
    assert temp_assets.delete_document_dir("doc-one") is False
    assert not document.exists()
    assert sibling.exists()


def test_workspace_delete_kills_real_process_tree_and_preserves_other_document(tmp_path):
    _reset_registries()
    target = other = None
    try:
        target_pid_file = tmp_path / "target-child.pid"
        other_pid_file = tmp_path / "other-child.pid"
        target = _start_process_tree(target_pid_file)
        other = _start_process_tree(other_pid_file)
        target_child_pid = _wait_for_file(target_pid_file)
        other_child_pid = _wait_for_file(other_pid_file)

        target_task = {
            "task_id": "material_tree_target",
            "created": time.time(),
            "done": False,
            "success": False,
            "proc": target,
            "document_ids": ["cf_tree_target"],
        }
        other_task = {
            "task_id": "material_tree_other",
            "created": time.time(),
            "done": False,
            "success": False,
            "proc": other,
            "document_ids": ["cf_tree_other"],
        }
        marker_platform._record_task_process_identity(target_task)
        marker_platform._record_task_process_identity(other_task)
        marker_platform.MATERIAL_TASKS[target_task["task_id"]] = target_task
        marker_platform.MATERIAL_TASKS[other_task["task_id"]] = other_task

        async def run_delete():
            transport = httpx.ASGITransport(app=marker_platform.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.delete("/api/material_documents/cf_tree_target")

        response = asyncio.run(run_delete())
        payload = response.json()

        assert response.status_code == 200
        assert payload["remaining_pids"] == []
        assert target.pid in payload["terminated_pids"]
        assert not psutil.pid_exists(target.pid)
        assert not psutil.pid_exists(target_child_pid)
        assert other.is_alive()
        assert psutil.pid_exists(other_child_pid)
        assert "material_tree_other" in marker_platform.MATERIAL_TASKS
    finally:
        _force_cleanup(target)
        _force_cleanup(other)
        _reset_registries()


def test_termination_cleans_known_orphan_after_worker_root_exits(tmp_path):
    target = None
    try:
        child_pid_file = tmp_path / "orphan-child.pid"
        target = _start_process_tree(child_pid_file)
        child_pid = _wait_for_file(child_pid_file)
        task = {"task_id": "material_orphan", "proc": target}
        marker_platform._record_task_process_identity(task)
        marker_platform._snapshot_task_process_tree(task)

        # Simulate a worker root disappearing before the API receives delete.
        target.terminate()
        target.join(3)
        assert not target.is_alive()
        assert psutil.pid_exists(child_pid)

        result = asyncio.run(marker_platform._terminate_task_process(task))

        assert result["remaining_pids"] == []
        assert not psutil.pid_exists(child_pid)
    finally:
        _force_cleanup(target)


def test_cancelled_prepare_releases_parent_slot_for_next_process(monkeypatch):
    _reset_registries()

    async def run_case():
        semaphore = asyncio.Semaphore(1)
        monkeypatch.setattr(marker_platform, "_ASYNC_CONVERSION_SLOTS", semaphore)

        async def fake_consume(task_id):
            task = marker_platform.PAPER_PREP_TASKS[task_id]
            while not task.get("cancel"):
                await asyncio.sleep(0.01)
            task.update({
                "cancelled": True, "done": True, "success": False,
                "stage": "已取消", "result": {},
            })

        monkeypatch.setattr(marker_platform, "_consume_paper_prep_task", fake_consume)

        def make_task(task_id):
            return {
                "task_id": task_id,
                "created": time.time(),
                "done": False,
                "success": False,
                "result": {},
                "proc": SpawnableFakeProcess(),
                "client_document_id": f"cf_{task_id}",
                "document_id": "",
                "_pdf_path": "",
                "cancel": False,
                "cancel_event": asyncio.Event(),
            }

        first = make_task("prep_slot_first")
        second = make_task("prep_slot_second")
        marker_platform.PAPER_PREP_TASKS[first["task_id"]] = first
        marker_platform.PAPER_PREP_TASKS[second["task_id"]] = second
        first["consumer"] = asyncio.create_task(marker_platform._run_paper_prepare_task(first["task_id"]))
        second["consumer"] = asyncio.create_task(marker_platform._run_paper_prepare_task(second["task_id"]))

        for _ in range(100):
            if first["proc"].started:
                break
            await asyncio.sleep(0.01)
        assert first["proc"].started == 1
        assert second["proc"].started == 0

        result = await marker_platform._cancel_paper_prepare_task(first["task_id"])
        assert result["ok"] is True

        for _ in range(100):
            if second["proc"].started:
                break
            await asyncio.sleep(0.01)
        assert second["proc"].started == 1
        assert first["proc"].is_alive() is False

        await marker_platform._cancel_paper_prepare_task(second["task_id"])
        await asyncio.gather(first["consumer"], second["consumer"])
        assert semaphore._value == 1

    asyncio.run(run_case())
    _reset_registries()
