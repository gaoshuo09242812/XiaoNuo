"""Scoped idle-sleep prevention for the enabled owner-PC supervisor only."""
from contextlib import contextmanager
import ctypes
import os

CONTINUOUS=0x80000000
SYSTEM_REQUIRED=0x00000001

def native_apply():
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    apply=kernel.SetThreadExecutionState
    apply.argtypes=[ctypes.c_uint];apply.restype=ctypes.c_uint
    return apply

@contextmanager
def keep_system_awake(*,apply=None,platform=None):
    if apply is None and (platform or os.name)!='nt':
        yield False;return
    apply=apply or native_apply()
    previous=apply(CONTINUOUS|SYSTEM_REQUIRED)
    if not previous:raise OSError('Windows keep-awake request failed')
    try:yield True
    finally:
        if not apply(previous|CONTINUOUS):raise OSError('Windows execution-state restore failed')
