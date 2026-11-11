import ctypes
from ctypes import wintypes
import os


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def crypt(data: bytes, *, decrypt=False) -> bytes:
    if os.name != "nt":
        raise OSError("Szyfrowanie ciasteczek wymaga Windows.")
    source = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    input_blob = Blob(len(data), source)
    output_blob = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob),
    ]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(input_blob), None, None, None, None, 1, ctypes.byref(output_blob)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output_blob.data, output_blob.size)
    finally:
        kernel32.LocalFree(output_blob.data)


def youtube_cookie_file(data: bytes) -> bytes:
    text = data.decode("utf-8-sig")
    if not text.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
        raise ValueError("Wybierz plik ciasteczek w formacie Netscape.")
    lines = []
    for line in text.splitlines():
        fields = line.removeprefix("#HttpOnly_").split("\t")
        if len(fields) != 7:
            continue
        domain = fields[0].lower().lstrip(".")
        if domain == "youtube.com" or domain.endswith(".youtube.com"):
            lines.append(line)
    if not lines:
        raise ValueError("Plik nie zawiera ciasteczek YouTube.")
    return ("# Netscape HTTP Cookie File\n" + "\n".join(lines) + "\n").encode("utf-8")
