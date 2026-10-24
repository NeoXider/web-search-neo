"""Small observations for the env action: decode a Chrome PNG and shrink it to a model-sized grid.

Standard library only (zlib): Chrome's captures are 8-bit RGB or RGBA, non-interlaced, which is
all this decoder reads. The shrink is a box average, so a 64x64 observation of a 640x480 canvas
keeps thin sprites as darker cells instead of dropping them the way nearest-neighbour would.
"""
from __future__ import annotations

import base64
import struct
import zlib

_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def decode_png(data: bytes) -> tuple[int, int, int, bytes]:
    """(width, height, channels, raw rows without filter bytes) of an 8-bit RGB/RGBA PNG."""
    if not data.startswith(_SIGNATURE):
        raise ValueError("not a PNG")
    position, idat = len(_SIGNATURE), []
    width = height = channels = 0
    while position < len(data):
        length, kind = struct.unpack(">I4s", data[position:position + 8])
        chunk = data[position + 8:position + 8 + length]
        position += 12 + length
        if kind == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
            if depth != 8 or color not in (2, 6) or interlace:
                raise ValueError(f"unsupported PNG (depth {depth}, colour type {color}, interlace {interlace})")
            channels = 3 if color == 2 else 4
        elif kind == b"IDAT":
            idat.append(chunk)
        elif kind == b"IEND":
            break
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    out = bytearray(stride * height)
    previous = bytearray(stride)
    for row in range(height):
        start = row * (stride + 1)
        kind = raw[start]
        line = bytearray(raw[start + 1:start + 1 + stride])
        for index in range(stride):
            left = line[index - channels] if index >= channels else 0
            up = previous[index]
            corner = previous[index - channels] if index >= channels else 0
            if kind == 1:
                line[index] = (line[index] + left) & 0xFF
            elif kind == 2:
                line[index] = (line[index] + up) & 0xFF
            elif kind == 3:
                line[index] = (line[index] + ((left + up) >> 1)) & 0xFF
            elif kind == 4:
                guess = left + up - corner
                pa, pb, pc = abs(guess - left), abs(guess - up), abs(guess - corner)
                pick = left if pa <= pb and pa <= pc else (up if pb <= pc else corner)
                line[index] = (line[index] + pick) & 0xFF
        out[row * stride:(row + 1) * stride] = line
        previous = line
    return width, height, channels, bytes(out)


def shrink(png: bytes, width: int, height: int, gray: bool = True) -> dict:
    """A width x height observation (box-averaged), gray (1 channel) or RGB (3), as base64 bytes."""
    src_w, src_h, channels, pixels = decode_png(png)
    width, height = max(1, min(int(width), 256)), max(1, min(int(height), 256))
    out = bytearray()
    for y in range(height):
        y0, y1 = y * src_h // height, max(y * src_h // height + 1, (y + 1) * src_h // height)
        for x in range(width):
            x0, x1 = x * src_w // width, max(x * src_w // width + 1, (x + 1) * src_w // width)
            total = [0, 0, 0]
            count = 0
            for sy in range(y0, y1):
                base = sy * src_w * channels
                for sx in range(x0, x1):
                    at = base + sx * channels
                    total[0] += pixels[at]
                    total[1] += pixels[at + 1]
                    total[2] += pixels[at + 2]
                    count += 1
            red, green, blue = (value // max(count, 1) for value in total)
            if gray:
                out.append((red * 299 + green * 587 + blue * 114) // 1000)
            else:
                out.extend((red, green, blue))
    return {"width": width, "height": height, "channels": 1 if gray else 3, "encoding": "base64-uint8-rowmajor",
            "data": base64.b64encode(bytes(out)).decode("ascii")}
