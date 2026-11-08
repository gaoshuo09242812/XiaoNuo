"""Owned child-process fixture; no models or network."""
import struct,sys,time
mode=sys.argv[1]
while True:
    header=sys.stdin.buffer.read(4)
    if not header:break
    size=struct.unpack('!I',header)[0];payload=sys.stdin.buffer.read(size)
    if mode=='hang':time.sleep(20)
    if mode=='crash':raise SystemExit(17)
    if mode=='oversize':sys.stdout.buffer.write(struct.pack('!BI',0,1_920_045))
    elif mode=='bad_status':sys.stdout.buffer.write(struct.pack('!BI',7,1)+b'x')
    elif mode=='partial':sys.stdout.buffer.write(b'\x00\x00')
    elif mode=='too_long':sys.stdout.buffer.write(struct.pack('!BI',1,0))
    else:sys.stdout.buffer.write(struct.pack('!BI',0,len(payload))+payload)
    sys.stdout.buffer.flush()
    if mode in {'oversize','bad_status','partial','too_long'}:break
