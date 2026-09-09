"""Windows Job Object 管理 Worker 与全部受管理后代"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


class WindowsJobError(RuntimeError):
    code = "agent.process_tree"


class _Basic(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _Io(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _Extended(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", _Io), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class WindowsJob:
    """由父进程持有句柄，关闭句柄会终止整个进程树"""

    def __init__(self):
        if sys.platform != "win32":
            raise WindowsJobError("当前系统不支持 Windows Job Object")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel = kernel
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(kernel, name)
            function.argtypes, function.restype = args, result
        self._handle = kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise WindowsJobError("CreateJobObjectW 失败")
        info = _Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000
        if not kernel.SetInformationJobObject(self._handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise WindowsJobError("SetInformationJobObject 失败")

    def assign(self, pid: int):
        process = self._kernel.OpenProcess(0x0100 | 0x0001, False, pid)
        if not process:
            raise WindowsJobError("OpenProcess 失败")
        try:
            if not self._kernel.AssignProcessToJobObject(self._handle, process):
                raise WindowsJobError("AssignProcessToJobObject 失败")
        finally:
            self._kernel.CloseHandle(process)

    def close(self):
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None
