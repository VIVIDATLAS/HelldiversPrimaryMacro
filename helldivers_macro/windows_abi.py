from __future__ import annotations

import ctypes


# WinDef.h: ULONG_PTR is an unsigned integer with pointer width. Using
# c_size_t directly avoids depending on another Win32 typedef alias whose
# Python mapping can vary independently of this ABI contract.
ULONG_PTR = ctypes.c_size_t
POINTER_BITS = ctypes.sizeof(ctypes.c_void_p) * 8
POINTER_MASK = (1 << POINTER_BITS) - 1


def normalize_ulong_ptr(value: int) -> int:
    """Return an unsigned value normalized to the native pointer width."""
    return int(value) & POINTER_MASK


def marker_matches(extra_info: int, marker: int) -> bool:
    return normalize_ulong_ptr(extra_info) == normalize_ulong_ptr(marker)


def structure_field_type(structure: type[ctypes.Structure], name: str) -> type:
    for field_name, field_type, *_rest in structure._fields_:
        if field_name == name:
            return field_type
    raise KeyError(name)
