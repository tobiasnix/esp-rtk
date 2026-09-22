#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Run a resumable synthetic tracking benchmark through USB Raw REPL."""
import argparse
import json

from rawrepl import with_retry

PREFIX = "scale-benchmark-v15"


def program(points, cleanup, segment_kib=128):
    return """import gc,os,time,tracking,pointstore,ujson
from machine import WDT
wdt=WDT(0,3600000)
p=%r
tracking.SEGMENT_MAX_BYTES=%d*1024
pointstore.POINT_SEGMENT_BYTES=%d*1024
def clean():
 for n in list(os.listdir()):
  if n.startswith(p):
   try: os.remove(n)
   except OSError: pass
if %r: clean()
t=tracking.Tracker(p)
tracking.MAX_FEATURES=max(tracking.TARGET_MAX_FEATURES,%d)
if not t.projects:
 t.create_project('Synthetic benchmark');t.start_line('Synthetic line',0)
f={'lat':40.0,'lon':10.0,'alt':100.0,'qual':4,'fix_status_text':'RTK_FIXED','sats':24,'hdop':0.6,'correction_age_sec':0.5,'station_id':'SYNTHETIC','receiver_accuracy':{'source':'NMEA_GST','semi_major_sigma_m':0.02,'altitude_sigma_m':0.04}}
done=t.feature_count();minimum=gc.mem_free();started=time.ticks_ms()
while done<%d:
 f['lon']=10.0+done*.000001;t.add_vertex(f,source='automatic');done+=1
 if done%%25==0: gc.collect();minimum=min(minimum,gc.mem_free());wdt.feed()
append_ms=time.ticks_diff(time.ticks_ms(),started);started=time.ticks_ms();t.compact();checkpoint_ms=time.ticks_diff(time.ticks_ms(),started)
started=time.ticks_ms();r=tracking.Tracker(p);start_ms=time.ticks_diff(time.ticks_ms(),started);started=time.ticks_ms();export_bytes=sum(len(x) for x in r.iter_backup_ndjson());export_ms=time.ticks_diff(time.ticks_ms(),started)
s=os.statvfs('/');print(ujson.dumps({'points':done,'append_total_ms':append_ms,
 'max_append_ms':t.max_append_ms,'checkpoint_ms':checkpoint_ms,'start_ms':start_ms,
 'export_ms':export_ms,'export_bytes':export_bytes,'heap_min':minimum,
 'heap_after':gc.mem_free(),'flash_free':s[0]*s[3],
 'append_profile_us':t.append_profile()}))
r.point_store.close();t.point_store.close()
if %r: clean()
""" % (PREFIX, segment_kib, segment_kib, cleanup, points, points, cleanup)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("points", type=int,
                        help="synthetic point target (500 profiles quickly; 5000+ are hardware gates)")
    parser.add_argument("--port")
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--segment-kib", type=int, choices=(32, 64, 128, 256),
                        default=128)
    args = parser.parse_args()
    if args.points < 1 or args.points > 50000:
        parser.error("points must be between 1 and 50000")
    try:
        output = with_retry(lambda repl: repl.ex(program(
            args.points, not args.keep_data, args.segment_kib), 7200), port=args.port, versuche=1,
            name="tracking benchmark", wdt_timeout_ms=3600000)
    except Exception:
        if not args.keep_data:
            cleanup = ("import os\nfor n in list(os.listdir()):\n"
                " if n.startswith(%r):\n  try: os.remove(n)\n  except OSError: pass\n" % PREFIX)
            try:
                with_retry(lambda repl: repl.ex(cleanup, 60), port=args.port,
                           versuche=3, name="benchmark cleanup")
            except Exception:
                pass
        raise
    result = json.loads(output.decode().strip().splitlines()[-1])
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
