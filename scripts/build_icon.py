"""Generate the application's geometric T icon using only the Python standard library."""
from pathlib import Path
import struct
import zlib

size = 256
rows = []
for y in range(size):
    row = bytearray([0])
    for x in range(size):
        dx, dy = max(32-x, 0, x-223), max(32-y, 0, y-223)
        inside = dx*dx + dy*dy <= 32*32
        white = (54 <= x <= 201 and 62 <= y <= 94) or (112 <= x <= 143 and 80 <= y <= 194)
        row.extend((244, 252, 249, 255) if inside and white else (24, 105, 83, 255) if inside else (0, 0, 0, 0))
    rows.append(row)
def chunk(kind, value):
    return struct.pack('!I', len(value)) + kind + value + struct.pack('!I', zlib.crc32(kind + value))
png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', size, size, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b'')
assets = Path(__file__).resolve().parents[1] / 'desktop/assets'
assets.mkdir(parents=True, exist_ok=True)
(assets / 'icon.png').write_bytes(png)
(assets / 'icon.ico').write_bytes(struct.pack('<3H', 0, 1, 1) + struct.pack('<4B2H2I', 0, 0, 0, 0, 1, 32, len(png), 22) + png)
