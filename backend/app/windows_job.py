"""Own only the worker process tree; close kills its descendants, never a PID scan."""
import ctypes
from ctypes import wintypes as w
import os

def api():
    if os.name!='nt':raise OSError('Windows Job Objects required')
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    for name,args,result in [
        ('CreateJobObjectW',[ctypes.c_void_p,w.LPCWSTR],w.HANDLE),
        ('OpenJobObjectW',[w.DWORD,w.BOOL,w.LPCWSTR],w.HANDLE),
        ('SetInformationJobObject',[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD],w.BOOL),
        ('AssignProcessToJobObject',[w.HANDLE,w.HANDLE],w.BOOL),
        ('GetCurrentProcess',[],w.HANDLE),('CloseHandle',[w.HANDLE],w.BOOL)]:
        f=getattr(k,name);f.argtypes=args;f.restype=result
    return k

class Basic(ctypes.Structure):
    _fields_=[('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),('flags',w.DWORD),
              ('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),('active',w.DWORD),
              ('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
class Counters(ctypes.Structure):
    _fields_=[(name,ctypes.c_ulonglong) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
class Extended(ctypes.Structure):
    _fields_=[('basic',Basic),('io',Counters),('process_limit',ctypes.c_size_t),('job_limit',ctypes.c_size_t),
              ('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]

class OwnedJob:
    def __init__(self,name):
        self.k=api();self.handle=self.k.CreateJobObjectW(None,name)
        if not self.handle:raise ctypes.WinError(ctypes.get_last_error())
        self.name=name
        info=Extended();info.basic.flags=0x2000 # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.k.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):
            error=ctypes.get_last_error();self.close();raise ctypes.WinError(error)
    def close(self):
        if self.handle:self.k.CloseHandle(self.handle);self.handle=None

def join_job(name):
    k=api();handle=k.OpenJobObjectW(1,False,name)
    if not handle:raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not k.AssignProcessToJobObject(handle,k.GetCurrentProcess()):raise ctypes.WinError(ctypes.get_last_error())
    finally:k.CloseHandle(handle)
