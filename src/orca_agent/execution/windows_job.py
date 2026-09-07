"""Small Win32 Job Object wrapper used by the local ORCA runner."""

from __future__ import annotations

import ctypes
import os
import platform
from ctypes import wintypes


class WindowsJobObject:
    """Own a process tree and kill it when the supervisor closes unexpectedly."""

    def __init__(self, *, memory_limit_bytes: int = 2048 * 1024 * 1024) -> None:
        if os.name != "nt":
            raise OSError("Windows Job Objects are only available on Windows")
        if type(memory_limit_bytes) is not int or memory_limit_bytes <= 0:
            raise ValueError("job memory limit must be positive")
        self.memory_limit_bytes = memory_limit_bytes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = self._kernel32.CreateJobObjectW
        create.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        create.restype = wintypes.HANDLE
        self._handle = create(None, None)
        if not self._handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        try:
            self._set_kill_on_close()
        except Exception:
            self.close()
            raise

    def _set_kill_on_close(self) -> None:
        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = 0x00002000 | 0x00000200
        info.JobMemoryLimit = self.memory_limit_bytes
        fn = self._kernel32.SetInformationJobObject
        fn.argtypes = [wintypes.HANDLE, wintypes.INT, ctypes.c_void_p, wintypes.DWORD]
        fn.restype = wintypes.BOOL
        if not fn(self._handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def assign_pid(self, pid: int) -> None:
        if type(pid) is not int or pid <= 0:
            raise ValueError("process PID must be positive")
        access = 0x001F0FFF  # PROCESS_ALL_ACCESS for the current user
        open_process = self._kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        process = open_process(access, False, pid)
        if not process:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            fn = self._kernel32.AssignProcessToJobObject
            fn.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            fn.restype = wintypes.BOOL
            if not fn(self._handle, process):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        finally:
            close_handle = self._kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL
            close_handle(process)

    def terminate(self, exit_code: int = 1) -> None:
        if self._handle:
            terminate = self._kernel32.TerminateJobObject
            terminate.argtypes = [wintypes.HANDLE, wintypes.UINT]
            terminate.restype = wintypes.BOOL
            if not terminate(self._handle, exit_code):
                raise OSError(ctypes.get_last_error(), "TerminateJobObject failed")
        else:
            raise OSError("Job Object is closed")

    def active_process_count(self) -> int:
        class Accounting(ctypes.Structure):
            _fields_ = [
                ("times", ctypes.c_longlong * 4),
                ("faults", wintypes.DWORD),
                ("total", wintypes.DWORD),
                ("active", wintypes.DWORD),
                ("terminated", wintypes.DWORD),
            ]

        info = Accounting()
        query = self._kernel32.QueryInformationJobObject
        query.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        query.restype = wintypes.BOOL
        if not query(self._handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise OSError(ctypes.get_last_error(), "QueryInformationJobObject failed")
        return int(info.active)

    def resume_pid(self, pid: int) -> None:
        """Resume the initial thread only after the suspended child joins this job."""

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        api = self._kernel32
        api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        api.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        api.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        api.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenThread.restype = wintypes.HANDLE
        api.ResumeThread.argtypes = [wintypes.HANDLE]
        api.ResumeThread.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        snapshot = api.CreateToolhelp32Snapshot(0x4, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "thread snapshot failed")
        resumed = 0
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            present = api.Thread32First(snapshot, ctypes.byref(entry))
            while present:
                if entry.th32OwnerProcessID == pid:
                    thread = api.OpenThread(0x2, False, entry.th32ThreadID)
                    if not thread:
                        raise OSError(ctypes.get_last_error(), "OpenThread failed")
                    try:
                        if api.ResumeThread(thread) != 1:
                            raise OSError("child initial thread was not suspended exactly once")
                        resumed += 1
                    finally:
                        api.CloseHandle(thread)
                present = api.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            api.CloseHandle(snapshot)
        if resumed != 1:
            raise OSError("expected one suspended initial thread")

    def close(self) -> None:
        if getattr(self, "_handle", None):
            close_handle = self._kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL
            close_handle(self._handle)
            self._handle = None

    def __enter__(self) -> WindowsJobObject:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()


def host_identity() -> str:
    return f"{platform.node()}:{os.name}"


def process_start_marker(pid: int) -> float | None:
    """Return a best-effort process creation marker without trusting PID alone."""

    if os.name != "nt":
        try:
            from pathlib import Path

            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return None if fields[0] == "Z" else float(fields[19])
        except (OSError, ValueError, IndexError):
            return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    process = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return None
    try:
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(process, ctypes.byref(code)) or code.value != 259:
            return None
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
            ctypes.POINTER(wintypes.FILETIME)
        ] * 4
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        created = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            process,
            ctypes.byref(created),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return ticks / 10_000_000.0
    finally:
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        close_handle(process)


__all__ = ["WindowsJobObject", "host_identity", "process_start_marker"]
