"""Windows Job Object hard limits; portable watchdog helpers for tests."""
import os

def apply(process,memory_mb=768,cpu_percent=20):
    if os.name!='nt':return None
    import ctypes
    from ctypes import wintypes as w
    class Basic(ctypes.Structure):
        _fields_=[('PerProcessUserTimeLimit',ctypes.c_longlong),('PerJobUserTimeLimit',ctypes.c_longlong),
            ('LimitFlags',w.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),('MaximumWorkingSetSize',ctypes.c_size_t),
            ('ActiveProcessLimit',w.DWORD),('Affinity',ctypes.c_size_t),('PriorityClass',w.DWORD),('SchedulingClass',w.DWORD)]
    class IO(ctypes.Structure):_fields_=[(name,ctypes.c_ulonglong) for name in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
    class Extended(ctypes.Structure):
        _fields_=[('BasicLimitInformation',Basic),('IoInfo',IO),('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
    class Cpu(ctypes.Structure):_fields_=[('ControlFlags',w.DWORD),('CpuRate',w.DWORD)]
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.CreateJobObjectW.restype=w.HANDLE;k.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR]
    k.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
    k.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
    k.CloseHandle.argtypes=[w.HANDLE]
    handle=k.CreateJobObjectW(None,None)
    if not handle:raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits=Extended();limits.BasicLimitInformation.LimitFlags=0x2000|0x100|0x200
        limits.ProcessMemoryLimit=limits.JobMemoryLimit=int(memory_mb)*1024*1024
        if not k.SetInformationJobObject(handle,9,ctypes.byref(limits),ctypes.sizeof(limits)):raise ctypes.WinError(ctypes.get_last_error())
        cpu=Cpu(1|4,max(1,min(100,int(cpu_percent)))*100)
        if not k.SetInformationJobObject(handle,15,ctypes.byref(cpu),ctypes.sizeof(cpu)):raise ctypes.WinError(ctypes.get_last_error())
        if not k.AssignProcessToJobObject(handle,w.HANDLE(int(process._handle))):raise ctypes.WinError(ctypes.get_last_error())
        return handle
    except BaseException:k.CloseHandle(handle);raise

def release(handle):
    if handle is not None:
        import ctypes
        k=ctypes.WinDLL('kernel32',use_last_error=True);k.CloseHandle.argtypes=[ctypes.c_void_p];k.CloseHandle(handle)

