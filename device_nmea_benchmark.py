#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Run the synthetic NMEA hot-path benchmark through USB Raw REPL.

One program source serves the device (this tool) and the host
(tests/benchmark_nmea_hotpath.py executes the same text), so a before/after
comparison measures the same code on the same inputs (#28c, #36). It times the
parsers and the gate check per call, the real GNSSHandler line scanner over a
synthetic sentence stream, and the RTCM paths, and it counts heap allocations
with gc.mem_alloc() on the device. It never reads field data and never touches
the UART or the flash: the receiver UART is replaced by an in-memory feed for
the duration of each scan.
"""
import argparse
import json

from rawrepl import with_retry

# Six minute decimals like the device validation rows 1a.14 and 1b.6.
GGA_BODY = "GNGGA,123519.00,4807.038123,N,01131.000456,E,4,24,0.6,545.412,M,46.903,M,1.0,0001"

# The RMC carries status V so that GNSSHandler never steps the RTC from
# synthetic data.
EPOCH_BODIES = (
    GGA_BODY,
    "GNGST,123519.00,0.5,0.012,0.010,45.0,0.011,0.010,0.025",
    "GNRMC,123519.00,V,4807.038123,N,01131.000456,E,0.02,45.00,011026,,,R,V",
    "GNGSA,A,3,01,02,03,04,05,06,07,08,09,10,11,12,1.2,0.6,1.0,1",
    "GNGSA,A,3,65,66,67,68,69,70,71,72,,,,,1.2,0.6,1.0,2",
    "GPGSV,4,1,14,01,40,083,46,02,17,308,41,03,07,344,39,04,22,228,45,1",
    "GPGSV,4,2,14,05,40,083,46,06,17,308,41,07,07,344,39,08,22,228,45,1",
    "GPGSV,4,3,14,09,40,083,46,10,17,308,41,11,07,344,39,12,22,228,45,1",
    "GPGSV,4,4,14,13,40,083,46,14,17,308,41,,,,,,,,,1",
    "GNVTG,45.00,T,,M,0.02,N,0.04,K,R",
)


def _checksum(body):
    value = 0
    for byte in body:
        value ^= byte
    return value


def _sentence(body, end=b"\r\n", checksum=None):
    body = body.encode("latin-1") if isinstance(body, str) else body
    value = _checksum(body) if checksum is None else checksum
    return b"$" + body + b"*" + (b"%02X" % value) + end


def _scanner_corpus():
    """Fixed byte stream for the scanner equivalence check (digest before/after).

    Valid sentences of every whitelist type, then every filter and error path
    of the line loop: non-ASCII bytes in whitelist and foreign sentences, a bad
    talker byte, a wrong checksum, line endings other than CRLF, empty lines,
    text without a sentence start, and an unterminated tail.
    """
    valid_vtg = "GNVTG,45.00,T,,M,0.02,N,0.04,K,R"
    valid_gsa = "GNGSA,A,3,01,02,03,04,05,06,07,08,09,10,11,12,1.2,0.6,1.0,1"
    lines = [_sentence(body) for body in EPOCH_BODIES]
    lines += [
        # A whitelist sentence with a 0xFF byte (invalid UTF-8) and one with a
        # valid UTF-8 pair; the checksum covers the bytes as sent.
        _sentence(b"GNGGA,\xff123519.00,4807.038123,N,01131.000456,E,4,24,0.6,545.412,M,46.903,M,1.0,0001"),
        _sentence(b"GNGSA,A,3,01,\xc3\xa4,03,04,05,06,07,08,09,10,11,12,1.2,0.6,1.0,1"),
        # A foreign sentence with a 0xFF byte, then plain text without a start.
        _sentence(b"GNXYZ,\xff,1"),
        b"garbage without a sentence start\r\n",
        # A bad talker byte in front of a whitelist type.
        _sentence(b"G\xffRMC,123519.00,V,4807.038123,N,01131.000456,E,0.02,45.00,011026,,,R,V"),
        # A wrong checksum on an otherwise valid sentence.
        _sentence(valid_vtg, checksum=(_checksum(valid_vtg.encode()) ^ 0x55)),
        # Line endings other than CRLF, empty lines.
        _sentence(valid_vtg, end=b"\n"),
        _sentence(valid_gsa, end=b"\r\r\n"),
        b"\r\r\n",
        b"\r\n",
        _sentence(GGA_BODY),
    ]
    # An unterminated sentence at the end never reaches the queue.
    return b"".join(lines) + b"$GNGGA,123519.00,4807"


SCANNER_CORPUS = _scanner_corpus()

# Placeholders (not %-formatting): the program text contains % itself.
PROGRAM = """import gc,sys,time
import ujson
from machine import WDT
wdt=WDT(0,600000)
GGA_BODY=@GGA@
CORPUS=@CORPUS@
EPOCH_BODIES=@EPOCH@
SENTENCES=@SENTENCES@
REPEAT=@REPEAT@
RTCM_BYTES=@RTCM@
MP=sys.implementation.name=='micropython'
BLOCK=500
SCAN_BLOCK_EPOCHS=100
RTCM_CHUNK=1024
RTCM_BLOCK_CHUNKS=4
if MP:
    clock=time.ticks_us
    def elapsed(a,b):
        return time.ticks_diff(b,a)
else:
    def clock():
        return time.perf_counter_ns()/1000
    def elapsed(a,b):
        return b-a

def mod(name):
    try:
        return __import__(name)
    except ImportError:
        return None

def crc_function():
    for name in ('ubinascii','binascii'):
        m=mod(name)
        f=getattr(m,'crc32',None) if m else None
        if f:
            return f
    return None

def lookup(dotted):
    if dotted=='ubinascii.crc32':
        return crc_function()
    parts=dotted.split('.')
    obj=mod(parts[0])
    for part in parts[1:]:
        if obj is None:
            return None
        obj=getattr(obj,part,None)
    return obj

def guarded(needs,fn):
    # A component of older firmware that is missing is reported, not fatal (E16).
    for name in needs:
        if lookup(name) is None:
            return {'unavailable':name}
    try:
        return fn()
    except Exception as e:
        return {'unavailable':'error: '+repr(e)}

def checksum_text(body):
    value=0
    for ch in body:
        value^=ord(ch)
    return '%02X' % value

def sentence_text(body):
    return '$'+body+'*'+checksum_text(body)

EPOCH=''.join([sentence_text(b)+'\\r\\n' for b in EPOCH_BODIES]).encode()
GGA_LINE=sentence_text(GGA_BODY)

def measure(fn,calls):
    # gc stays off inside a block, so the gc.mem_alloc() difference is the
    # gross allocation of the block (device only; the host has no such counter).
    total=0.0
    alloc=0
    done=0
    while done<calls:
        n=min(BLOCK,calls-done)
        gc.collect()
        if MP:
            gc.disable()
            before=gc.mem_alloc()
        try:
            started=clock()
            for _ in range(n):
                fn()
            finished=clock()
            if MP:
                alloc+=gc.mem_alloc()-before
        finally:
            # A MemoryError inside the block must not leave gc disabled.
            if MP:
                gc.enable()
        total+=elapsed(started,finished)
        wdt.feed()
        done+=n
    gc.collect()
    return {'calls':calls,'us_per_call':round(total/calls,2),
            'alloc_bytes_per_call':(alloc//calls if MP else None)}

class Sink:
    def __init__(self,keep):
        self.n=0
        self.keep=keep
        self.items=[]
    def put_nowait(self,item):
        self.n+=1
        if self.keep:
            self.items.append(item)
        return True
    def qsize(self):
        return self.n

class Feed:
    # Stands in for the receiver UART: delivers size bytes per read.
    def __init__(self,data,size):
        self.data=data
        self.size=size
        self.pos=0
    def any(self):
        return min(self.size,len(self.data)-self.pos)
    def read(self,n=None):
        chunk=self.data[self.pos:self.pos+self.size]
        self.pos+=len(chunk)
        return chunk or None
    def write(self,data):
        return len(data)
    def deinit(self):
        pass

def scan(data,size,keep):
    # Runs the real GNSSHandler.run() over data and restores every patched global.
    gnss=mod('gnss')
    app=gnss.app
    shutdown_event=gnss.shutdown_event
    aio=gnss.asyncio
    config=gnss.CONFIG
    sink=Sink(keep)
    feed=Feed(data,size)
    seen=[0,0]
    def update_fix(fix):
        seen[0]+=1
    def attach_gst(gst):
        seen[1]+=1
        return True
    async def sleep_ms(ms):
        if feed.pos>=len(feed.data):
            shutdown_event.set()
    old_uart=gnss.UART
    old_sleep=aio.sleep_ms
    old_limit=config['gsv_limit_per_sec']
    old_stats=dict(app.stats)
    old_methods=(app.update_fix,app.attach_gst)
    result=None
    try:
        gnss.UART=lambda *a,**k: feed
        aio.sleep_ms=sleep_ms
        config['gsv_limit_per_sec']=10**6
        app.update_fix=update_fix
        app.attach_gst=attach_gst
        shutdown_event.clear()
        handler=gnss.GNSSHandler(sink)
        crc_before=app.stats.get('crc_errors',0)
        gc.collect()
        if MP:
            gc.disable()
            before=gc.mem_alloc()
        started=clock()
        aio.run(handler.run())
        finished=clock()
        alloc=(gc.mem_alloc()-before) if MP else None
        result={'us':elapsed(started,finished),'alloc':alloc,'n':sink.n,
                'items':sink.items,'gga':seen[0],
                'crc':app.stats.get('crc_errors',0)-crc_before}
    finally:
        if MP:
            gc.enable()
        gnss.UART=old_uart
        aio.sleep_ms=old_sleep
        config['gsv_limit_per_sec']=old_limit
        shutdown_event.clear()
        for name,method in zip(('update_fix','attach_gst'),old_methods):
            try:
                delattr(app,name)
            except Exception:
                setattr(app,name,method)
        app.stats.clear()
        app.stats.update(old_stats)
    return result

def component_parse_gga():
    gnss=mod('gnss')
    class Bare(gnss.GNSSHandler):
        def __init__(self):
            pass
    parse=Bare().parse_gga
    return measure(lambda: parse(GGA_LINE),REPEAT)

def component_coord():
    parse=mod('geo').parse_nmea_coord_e8
    return measure(lambda: parse('4807.038123','N'),REPEAT)

def component_decimal():
    parse=mod('geo').parse_decimal_fixed
    return measure(lambda: parse('545.412',3),REPEAT)

def component_quality_check():
    gnss=mod('gnss')
    class Bare(gnss.GNSSHandler):
        def __init__(self):
            pass
    fix=Bare().parse_gga(GGA_LINE)
    fix['receiver_accuracy']={'source':'NMEA_GST','semi_major_sigma_m':0.012,
                              'altitude_sigma_m':0.025}
    check=mod('tracking').quality_check
    return measure(lambda: check(fix),REPEAT)

def component_checksum():
    check=mod('gnss').GNSSHandler.check_nmea_checksum
    return measure(lambda: check(GGA_LINE),REPEAT)

def component_scanner():
    epochs=SENTENCES//10
    total_us=0.0
    total_alloc=0
    items=gga=crc=0
    done=0
    while done<epochs:
        count=min(SCAN_BLOCK_EPOCHS,epochs-done)
        data=EPOCH*count
        r=scan(data,512,False)
        total_us+=r['us']
        if MP:
            total_alloc+=r['alloc']
        items+=r['n']
        gga+=r['gga']
        crc+=r['crc']
        done+=count
        wdt.feed()
        gc.collect()
    sentences=epochs*10
    return {'sentences':sentences,'us_per_sentence':round(total_us/sentences,2),
            'alloc_bytes_per_sentence':(total_alloc//sentences if MP else None),
            'queue_items':items,'gga_parsed':gga,'crc_errors':crc}

def component_corpus():
    crc32=crc_function()
    r=scan(CORPUS,7,True)
    digest=0
    size=0
    for item in r['items']:
        digest=crc32(item,digest)
        size+=len(item)
    return {'queue_items':r['n'],'queue_bytes':size,'crc_errors':r['crc'],'digest':digest}

def rtcm_chunks():
    # Frames of message 1077 (MSM7 header): D3, length 300, payload, three CRC bytes.
    frame=b'\\xd3\\x01\\x2c'+b'\\x43\\x50'+bytes(298)+bytes(3)
    stream=(frame*(RTCM_BYTES//len(frame)+1))[:RTCM_BYTES]
    return [stream[i:i+RTCM_CHUNK] for i in range(0,RTCM_BYTES,RTCM_CHUNK)]

def measure_chunks(step,chunks,reset):
    total=0.0
    alloc=0
    nbytes=0
    for chunk in chunks:
        nbytes+=len(chunk)
    i=0
    while i<len(chunks):
        block=chunks[i:i+RTCM_BLOCK_CHUNKS]
        gc.collect()
        if MP:
            gc.disable()
            before=gc.mem_alloc()
        try:
            started=clock()
            for chunk in block:
                step(chunk)
            finished=clock()
            if MP:
                alloc+=gc.mem_alloc()-before
        finally:
            if MP:
                gc.enable()
        total+=elapsed(started,finished)
        reset()
        wdt.feed()
        i+=RTCM_BLOCK_CHUNKS
    gc.collect()
    kb=nbytes/1024
    return {'bytes':nbytes,'us_per_kb':round(total/kb,2),
            'alloc_bytes_per_kb':(int(alloc/kb) if MP else None)}

def component_rtcm_feed():
    counter=mod('net').RtcmCounter()
    return measure_chunks(counter.feed,rtcm_chunks(),lambda: None)

def component_observe_rtcm():
    recorder=mod('field_diagnostics').FieldRecorder()
    recorder.enabled=True
    def reset():
        recorder.rtcm_queue=[]
        recorder.rtcm_bytes=0
    return measure_chunks(recorder.observe_rtcm,rtcm_chunks(),reset)

def component_uart_tx():
    class Null:
        def write(self,data):
            return len(data)
    tx=mod('gnss').UartTx(Null())
    return measure_chunks(tx.write_rtcm,rtcm_chunks(),lambda: None)

out={'runtime':'micropython' if MP else 'cpython'}
cfg=mod('cfg')
# parse_gga and the scanner write into app.stats; put it back when done.
app_state=mod('state')
stats0=dict(app_state.app.stats) if app_state else None
out['version']=cfg.CONFIG['version'] if cfg else None
PARSE=('gnss.GNSSHandler.parse_gga','geo.parse_nmea_coord_e8','geo.parse_decimal_fixed')
out['parse_gga']=guarded(PARSE,component_parse_gga)
out['coord_e8']=guarded(('geo.parse_nmea_coord_e8',),component_coord)
out['decimal_mm']=guarded(('geo.parse_decimal_fixed',),component_decimal)
out['quality_check']=guarded(('tracking.quality_check',)+PARSE,component_quality_check)
out['checksum']=guarded(('gnss.GNSSHandler.check_nmea_checksum',),component_checksum)
out['scanner']=guarded(('gnss.GNSSHandler',)+PARSE,component_scanner)
out['scanner_corpus']=guarded(('gnss.GNSSHandler','ubinascii.crc32')+PARSE,component_corpus)
out['rtcm_feed']=guarded(('net.RtcmCounter',),component_rtcm_feed)
out['observe_rtcm']=guarded(('field_diagnostics.FieldRecorder.observe_rtcm',),component_observe_rtcm)
out['uart_tx_write_rtcm']=guarded(('gnss.UartTx',),component_uart_tx)
if stats0 is not None:
    app_state.app.stats.clear()
    app_state.app.stats.update(stats0)
print(ujson.dumps(out))
"""


def program(sentences=10000, repeat=500, rtcm_bytes=65536):
    if sentences < 10 or repeat < 1 or rtcm_bytes < 1024:
        raise ValueError("sentences >= 10, repeat >= 1 and rtcm_bytes >= 1024 required")
    return (PROGRAM.replace("@GGA@", repr(GGA_BODY))
            .replace("@CORPUS@", repr(SCANNER_CORPUS))
            .replace("@EPOCH@", repr(EPOCH_BODIES))
            .replace("@SENTENCES@", str(int(sentences)))
            .replace("@REPEAT@", str(int(repeat)))
            .replace("@RTCM@", str(int(rtcm_bytes))))


def _at_least(minimum):
    def parse(text):
        value = int(text)
        if value < minimum:
            raise argparse.ArgumentTypeError("must be at least %d" % minimum)
        return value
    return parse


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--sentences", type=_at_least(10), default=10000,
                        help="sentences streamed through the line scanner (10 per epoch)")
    parser.add_argument("--repeat", type=_at_least(1), default=500,
                        help="calls per parser/gate component")
    args = parser.parse_args(argv)
    output = with_retry(lambda repl: repl.ex(program(args.sentences, args.repeat), 900),
                        port=args.port, versuche=1, name="nmea benchmark",
                        wdt_timeout_ms=600000)
    result = json.loads(output.decode().strip().splitlines()[-1])
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
