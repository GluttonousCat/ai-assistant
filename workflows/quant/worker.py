"""Single OS-locked durable workspace consumer, independent of HTTP requests."""

import asyncio
import argparse
import os
import subprocess
import sys

from core.config import get_config
from core.logger import get_logger
from skills.quant.workspace_runner import run_task
from tools.market import quant_workspace as store

logger = get_logger(__name__)


def execute_task(task):
    key = task["id"]
    try:
        result = run_task(task, lambda **fields: store.update_task(key, **fields))
        store.save_result(key, result)
        store.update_task(
            key, status="completed", progress="回测完成", metrics=result["metrics"]
        )
    except Exception as error:
        logger.exception("工作台回测失败 %s", key)
        message = (
            str(error)
            if isinstance(error, ValueError)
            else "运行失败，请检查后台日志及本地数据库连接后重试"
        )
        store.update_task(key, status="failed", progress=message[:1000])


def process_queue():
    """Synchronous single-unit entry for CLI/tests; the backend supervises a child."""
    with store.worker_lock() as acquired:
        if not acquired:
            return
        store.recover_tasks()
        task = store.next_task()
        if task:
            store.update_task(task["id"], status="running", progress="校验本地数据")
            execute_task(task)


async def _terminate(process):
    if process.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        await killer.wait()
    else:
        process.kill()
    await process.wait()


async def consume(stop):
    with store.worker_lock() as acquired:
        if not acquired or stop.is_set():
            return
        store.recover_tasks()
        task = store.next_task()
        if not task:
            return
        key = task["id"]
        store.update_task(key, status="running", progress="校验本地数据")
        process = None
        waiters = []
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "workflows.quant.worker",
                "--execute",
                key,
                "--owner",
                task["owner"],
                cwd=get_config().root_dir,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                **(
                    {"creationflags": subprocess.CREATE_NO_WINDOW}
                    if os.name == "nt"
                    else {}
                ),
            )
            finished = asyncio.create_task(process.wait())
            stopping = asyncio.create_task(stop.wait())
            waiters = [finished, stopping]
            timeout = max(
                10,
                min(
                    3600,
                    float(get_config().get("quant.workspace_timeout_seconds", 900)),
                ),
            )
            done, _ = await asyncio.wait(
                waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if finished not in done:
                await _terminate(process)
                store.update_task(
                    key,
                    status="interrupted" if stop.is_set() else "failed",
                    progress="服务关闭，回测已中断，可重新提交"
                    if stop.is_set()
                    else "回测超时，已释放队列；请检查数据库连接或减小运行规模",
                )
            elif store.task(int(task["owner"]), key)["status"] == "running":
                store.update_task(
                    key,
                    status="failed",
                    progress="回测进程意外结束，请检查后台日志后重试",
                )
        except Exception:
            logger.exception("回测工作进程调度失败 %s", key)
            store.update_task(
                key,
                status="failed",
                progress="回测工作进程启动或调度失败，请检查后台日志后重试",
            )
        finally:
            if process and process.returncode is None:
                await _terminate(process)
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)


async def run_worker(stop):
    while not stop.is_set():
        try:
            await consume(stop)
        except Exception:
            logger.exception("量化工作台队列检查失败")
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute")
    parser.add_argument("--owner", type=int)
    args = parser.parse_args()
    get_config()
    if args.execute:
        row = store.task(args.owner, args.execute)
        if not row or row["status"] != "running":
            raise SystemExit("Task not running")
        execute_task(row)
    else:
        process_queue()
