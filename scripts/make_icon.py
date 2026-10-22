"""Generate the project's small geometric T icon without image dependencies."""
from pathlib import Path
import struct
import zlib


def png(size):
    rows=[]
    for y in range(size):
        row=bytearray([0])
        for x in range(size):
            inside=2<=x<size-2 and 2<=y<size-2
            letter=(.23*size<=x<.77*size and .25*size<=y<.40*size) or (.43*size<=x<.57*size and .35*size<=y<.78*size)
            row.extend((255,255,255,255) if letter else (23,101,86,255) if inside else (0,0,0,0))
        rows.append(bytes(row))
    def chunk(kind,data):return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',size,size,8,6,0,0,0))+chunk(b'IDAT',zlib.compress(b''.join(rows)))+chunk(b'IEND',b'')


if __name__=='__main__':
    sizes=(16,20,24,32,48,64,256)
    images=[png(size) for size in sizes]
    offset=6+16*len(sizes)
    result=struct.pack('<HHH',0,1,len(sizes))
    for size,data in zip(sizes,images):
        result+=struct.pack('<BBBBHHII',size%256,size%256,0,0,1,32,len(data),offset)
        offset+=len(data)
    (Path(__file__).resolve().parents[1]/'static/teemerchant.ico').write_bytes(result+b''.join(images))
