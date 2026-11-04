import asyncio
import os
import signal
from contextlib import suppress
from pathlib import Path

from .contracts import MediaError

MAX_INPUT = 200 * 1024 * 1024
MAX_WORK = 2 * MAX_INPUT
MAX_DURATION = 20 * 60


class ProcessFailure(MediaError):
    def __init__(self, diagnostics: str):
        super().__init__("The media could not be processed.")
        self.diagnostics = diagnostics


def work_size(folder: Path) -> int:
    size = 0
    for item in folder.iterdir():
        with suppress(FileNotFoundError):
            size += item.stat().st_size
    return size


async def stop_process(process: asyncio.subprocess.Process) -> None:
    if os.name == "nt":
        if process.returncode is None:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


async def run(
    command: list[str],
    folder: Path,
    *,
    seconds: int,
    output_limit: int = 8 * 1024 * 1024,
) -> str:
    stdout = folder / "process.out"
    stderr = folder / "process.err"
    env = {
        key: os.environ[key]
        for key in ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL")
        if key in os.environ
    }
    env.update(
        {"TEMP": str(folder), "TMP": str(folder), "TMPDIR": str(folder), "NO_PROXY": ""}
    )
    try:
        with stdout.open("wb") as out, stderr.open("wb") as err:
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    cwd=folder,
                    env=env,
                    start_new_session=os.name != "nt",
                )
            )
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                if not spawn.done() or spawn.exception() is None:
                    await stop_process(await spawn)
                raise
            waiter = asyncio.create_task(process.wait())
            try:
                async with asyncio.timeout(seconds):
                    while not waiter.done():
                        await asyncio.wait({waiter}, timeout=0.35)
                        size = await asyncio.to_thread(work_size, folder)
                        if size > MAX_WORK or stdout.stat().st_size > output_limit:
                            raise MediaError(
                                "The media exceeds the processing size limit."
                            )
            finally:
                if not waiter.done():
                    await stop_process(process)
                await waiter
        if process.returncode:
            with stderr.open("rb") as diagnostics:
                diagnostics.seek(max(0, stderr.stat().st_size - 6000))
                raise ProcessFailure(
                    diagnostics.read().decode("utf-8", errors="replace")
                )
        return stdout.read_text(encoding="utf-8", errors="replace")
    except TimeoutError as exc:
        raise MediaError(
            "Processing timed out. Try a shorter file or lower quality."
        ) from exc
    except FileNotFoundError as exc:
        raise MediaError(
            "A server dependency is missing. Check the installation guide."
        ) from exc
    finally:
        stdout.unlink(missing_ok=True)
        stderr.unlink(missing_ok=True)
