#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic one-Hz device benchmark including event-loop responsiveness."""
import argparse
import json

from rawrepl import with_retry

PREFIX = "realtime-benchmark-v15"


def program(points, cleanup):
    return """import gc,os,time,ujson,uasyncio as asyncio,tracking
from machine import WDT
wdt=WDT(0,3600000);p=%r
def clean():
 for n in list(os.listdir()):
  if n.startswith(p):
   try:os.remove(n)
   except OSError:pass
if %r:clean()
t=tracking.Tracker(p);tracking.MAX_FEATURES=tracking.TARGET_MAX_FEATURES
t.create_project('Synthetic realtime benchmark');t.start_line('Synthetic line',0)
f={'lat':40.0,'lon':10.0,'alt':100.0,'qual':4,'fix_status_text':'RTK_FIXED','sats':24,'hdop':0.6,'correction_age_sec':0.5,'station_id':'SYNTHETIC','receiver_accuracy':{'source':'NMEA_GST','semi_major_sigma_m':0.02,'altitude_sigma_m':0.04}}
lat=[];lag=[];running=True;minimum=gc.mem_free()
async def heartbeat():
 global minimum
 due=time.ticks_add(time.ticks_ms(),100)
 while running:
  await asyncio.sleep_ms(100);now=time.ticks_ms();lag.append(max(0,time.ticks_diff(now,due)));due=time.ticks_add(now,100);minimum=min(minimum,gc.mem_free());wdt.feed()
async def record():
 global running
 due=time.ticks_ms()
 for n in range(%d):
  f['lon']=10.0+n*.000001;started=time.ticks_ms();t.add_vertex(f,source='automatic');lat.append(max(0,time.ticks_diff(time.ticks_ms(),started)))
  due=time.ticks_add(due,1000);await asyncio.sleep_ms(max(0,time.ticks_diff(due,time.ticks_ms())))
 running=False
async def main():
 h=asyncio.create_task(heartbeat());await record();await h;started=time.ticks_ms();await t.compact_async();return time.ticks_diff(time.ticks_ms(),started)
checkpoint_ms=asyncio.run(main());lat.sort();lag.sort();s=os.statvfs('/')
def pct(v,n):return v[min(len(v)-1,(len(v)*n)//100)] if v else 0
print(ujson.dumps({'points':t.feature_count(),'append_p50_ms':pct(lat,50),'append_p95_ms':pct(lat,95),'append_p99_ms':pct(lat,99),'append_max_ms':max(lat) if lat else 0,'loop_lag_p99_ms':pct(lag,99),'loop_lag_max_ms':max(lag) if lag else 0,'checkpoint_ms':checkpoint_ms,'heap_min':minimum,'heap_after':gc.mem_free(),'flash_free':s[0]*s[3],'append_profile_us':t.append_profile()}))
t.point_store.close()
if %r:clean()
""" % (PREFIX, cleanup, points, cleanup)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("points", type=int, choices=(60, 300, 600))
    parser.add_argument("--port")
    parser.add_argument("--keep-data", action="store_true")
    args = parser.parse_args()
    cleanup = not args.keep_data
    try:
        output = with_retry(lambda repl: repl.ex(program(args.points, cleanup),
            args.points + 600), port=args.port, versuche=1,
            name="one-Hz tracking benchmark", wdt_timeout_ms=3600000)
    except BaseException:
        if cleanup:
            code = ("import os\nfor n in list(os.listdir()):\n"
                    " if n.startswith(%r):\n  try:os.remove(n)\n  except OSError:pass\n" % PREFIX)
            try:
                with_retry(lambda repl: repl.ex(code, 60), port=args.port,
                           versuche=3, name="realtime benchmark cleanup")
            except Exception:
                pass
        raise
    print(json.dumps(json.loads(output.decode().strip().splitlines()[-1]),
                     sort_keys=True))


if __name__ == "__main__":
    main()
