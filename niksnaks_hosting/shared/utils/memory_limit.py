"""Impose a memory ceiling on a native server process.

A Java server gets ``-Xmx`` and the JVM enforces its own heap ceiling. Bedrock Dedicated
Server is a native binary with no such flag, so the equivalent ceiling has to come from
the operating system:

* Windows -- a job object with ``ProcessMemoryLimit``, which caps committed memory.
* POSIX   -- ``ulimit -d``, which since Linux 4.7 covers anonymous mappings and so tracks
  heap growth rather than reserved address space.

Both cap roughly what ``-Xmx`` caps. Neither collects garbage the way a JVM does: a
process that genuinely needs more than the ceiling fails its allocation and dies, so the
ceiling wants headroom over the server's real working set.
"""

from __future__ import annotations

import shlex
import sys

MIN_ENFORCEABLE_MB = 256


def wrap_command(command: list[str], limit_mb: int) -> list[str]:
    """Re-exec ``command`` under a POSIX data-segment limit. A no-op elsewhere.

    ``exec`` replaces the shell, so the server keeps the pipes and pid the caller expects.
    """
    if sys.platform == "win32" or limit_mb < MIN_ENFORCEABLE_MB or not command:
        return command

    quoted = " ".join(shlex.quote(part) for part in command)
    return ["/bin/sh", "-c", f"ulimit -d {limit_mb * 1024} 2>/dev/null; exec {quoted}"]


def apply_to_process(pid: int, limit_mb: int):
    """Cap a running Windows process. Returns a handle the caller must keep alive, or None.

    The limit lasts as long as the process stays in the job object. The job is created
    without ``KILL_ON_JOB_CLOSE`` so that closing the handle -- or quitting the app --
    never takes a running server down with it.
    """
    if sys.platform != "win32" or limit_mb < MIN_ENFORCEABLE_MB:
        return None

    import ctypes
    from ctypes import wintypes

    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_PROCESS_MEMORY
    info.ProcessMemoryLimit = limit_mb * 1024 * 1024

    if not kernel32.SetInformationJobObject(
        job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
    ):
        kernel32.CloseHandle(job)
        return None

    handle = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    if not handle:
        kernel32.CloseHandle(job)
        return None

    assigned = kernel32.AssignProcessToJobObject(job, handle)
    kernel32.CloseHandle(handle)

    if not assigned:
        kernel32.CloseHandle(job)
        return None

    return job


def release(job) -> None:
    if job and sys.platform == "win32":
        import ctypes

        try:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(job)
        except Exception:
            pass
