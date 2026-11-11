import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile

from .runtime import HIDE_WINDOW, atomic_json, command


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimits), ("io", ctypes.c_ulonglong * 6),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t),
    ]


class WindowsJob:
    def __init__(self, pid):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                    wintypes.DWORD]
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        process = kernel.OpenProcess(0x0100 | 0x0001, False, pid)
        try:
            if (not process
                    or not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits),
                                                          ctypes.sizeof(limits))
                    or not kernel.AssignProcessToJobObject(self.handle, process)):
                error = ctypes.WinError(ctypes.get_last_error())
                self.close()
                raise error
        finally:
            if process:
                kernel.CloseHandle(process)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class Worker:
    def __init__(self, directory: Path, request):
        directory.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="worker-", dir=directory, ignore_cleanup_errors=True)
        self.work = Path(self.temp.name)
        self.offset = 0
        self.trailing = b""
        self.process = None
        self.windows_job = None
        try:
            self.process = subprocess.Popen(command("--worker", self.work),
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, creationflags=HIDE_WINDOW,
                                            start_new_session=os.name != "nt")
            if os.name == "nt":
                self.windows_job = WindowsJob(self.process.pid)
            # The request also releases the child after it has been assigned to the job.
            atomic_json(self.work / "request.json", request)
        except Exception:
            self.close()
            raise

    def poll(self):
        return self.process.poll()

    def events(self):
        path = self.work / "events.jsonl"
        if not path.is_file():
            return
        with path.open("rb") as stream:
            stream.seek(self.offset)
            chunk = stream.read()
            self.offset += len(chunk)
        *lines, self.trailing = (self.trailing + chunk).split(b"\n")
        for line in lines:
            yield json.loads(line)

    def stop(self):
        if self.windows_job:
            self.windows_job.close()
        elif self.process and self.process.poll() is None:
            if os.name == "nt":
                self.process.kill()
            else:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def close(self):
        self.stop()
        if self.process:
            self.process.wait(timeout=5)
        self.temp.cleanup()
