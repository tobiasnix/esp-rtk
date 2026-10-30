#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Run the synthetic GeoJSON/backup export benchmark through USB Raw REPL.

Builds a synthetic project on the device (never reads field data), then streams
it once as GeoJSON and once as backup NDJSON, each in a timing run without heap
probes and in a heap run. The result splits the GeoJSON time into flash read,
decode and serialization and compares both formats (#42).
"""
import argparse
import json
import sys

from rawrepl import with_retry

PREFIX = "export-benchmark-v1"
# case -> (lines, vertices per line, object points); same sizes as tests/export_case.py
CASES = {"large": (4, 850, 131), "long": (1, 3400, 0)}
# Flash needed per feature (journal, point store, checkpoint): about 360 bytes
# measured on the host for the large case, with a wide margin; the free-space
# check adds the 20 percent flash reserve on top.
BYTES_PER_FEATURE = 1500


def program(case, cleanup, sample_every=1):
    lines, per_line, points = CASES[case]
    return """import gc,os,time,tracking,ujson
from machine import WDT
wdt=WDT(0,3600000)
p=%r
def clean():
 for n in list(os.listdir()):
  if n.startswith(%r):
   try: os.remove(n)
   except OSError: pass
if %r: clean()
s=os.statvfs('/')
if s[0]*s[3]<%d+s[0]*s[2]//5+262144: print(ujson.dumps({'insufficient_space':True}))
else:
 t=tracking.Tracker(p)
 if not t.projects:
  pid=t.create_project('Synthetic export')['id']
  f={'lat':40.0,'lon':10.0,'alt':100.0,'lat_e8':4000000000,'lon_e8':1000000000,'alt_mm':100000,'qual':4,'fix_status_text':'RTK_FIXED','sats':24,'hdop':0.6,'correction_age_sec':0.5,'station_id':'SYNTHETIC','receiver_accuracy':{'source':'NMEA_GST','semi_major_sigma_m':0.02,'altitude_sigma_m':0.04}}
  steps=0
  for n in range(%d):
   t.start_line('Synthetic line %%d'%%(n+1),0)
   f['lat_e8']=4000000000+n*1000
   for i in range(%d):
    f['lon_e8']=1000000000+i*100;f['alt_mm']=100000+i%%7*10;t.add_vertex(f,source='automatic');steps+=1
    if steps%%25==0: gc.collect();wdt.feed()
   t.set_line_state('finish')
  f['lat_e8']=4000500000;T=[x for x in tracking.OBJECT_TYPES if x!='stakeout']
  for k in range(%d):
   f['lon_e8']=1000500000+k*100;f['alt_mm']=100000+k%%5*10
   t.add_asset(T[k%%len(T)],'P-%%d'%%(k+1),'',f,False);steps+=1
   if steps%%25==0: gc.collect();wdt.feed()
  t.compact()
 t.point_store.close();del t;gc.collect()
 r=tracking.Tracker(p);pid=r.projects[0]['id']
 def timed(make):
  u=r.point_store.profile_us;r0=u['read'];d0=u['decode'];n=0;b=0;g=make();wdt.feed();a=time.ticks_ms()
  for c in g: n+=1;b+=len(c)
  ms=time.ticks_diff(time.ticks_ms(),a);ru=u['read']-r0;du=u['decode']-d0
  return {'ms':ms,'bytes':b,'chunks':n,'read_us':ru,'decode_us':du,'serialize_ms':round(max(0,ms-(ru+du)/1000),1)}
 def heap(make,every):
  gc.collect();idle=gc.mem_free();low=idle;n=0;g=make();wdt.feed()
  for c in g:
   n+=1
   if n%%every==0:
    m=gc.mem_free()
    if m<low: low=m
  return {'heap_idle':idle,'heap_min':low,'heap_peak_bytes':idle-low}
 out={'case':%r,'vertices':%d,'longest_line':%d}
 for name,make in (('geojson',lambda:r.iter_geojson(pid)),('backup',lambda:r.iter_backup_ndjson(pid))):
  res=timed(make);res.update(heap(make,%d));out[name]=res
 out['ratio_ms']=round(out['geojson']['ms']/max(1,out['backup']['ms']),2)
 out['heap_ratio']=round(out['geojson']['heap_peak_bytes']/max(1,out['backup']['heap_peak_bytes']),2)
 out['serialize_ms_geojson']=out['geojson']['serialize_ms']
 print(ujson.dumps(out))
 r.point_store.close()
 if %r: clean()
""" % (PREFIX + "-" + case, PREFIX, cleanup, (lines * per_line + points) * BYTES_PER_FEATURE,
       lines, per_line, points, case, lines * per_line, per_line, sample_every, cleanup)


def _sample_every(text):
    value = int(text)
    if not 1 <= value <= 100:
        raise argparse.ArgumentTypeError("must be between 1 and 100")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("case", choices=sorted(CASES),
                        help="large = device case (4 lines of 850 vertices + 131 points), "
                             "long = one line of 3400 vertices")
    parser.add_argument("--port", required=True)
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--sample-every", type=_sample_every, default=1,
                        help="heap probe after every Nth chunk (1-100)")
    args = parser.parse_args(argv)
    try:
        output = with_retry(lambda repl: repl.ex(program(
            args.case, not args.keep_data, args.sample_every), 7200), port=args.port,
            versuche=1, name="export benchmark", wdt_timeout_ms=3600000)
    except Exception:
        if not args.keep_data:
            cleanup = ("import os\nfor n in list(os.listdir()):\n"
                " if n.startswith(%r):\n  try: os.remove(n)\n  except OSError: pass\n" % PREFIX)
            try:
                with_retry(lambda repl: repl.ex(cleanup, 60), port=args.port,
                           versuche=3, name="export benchmark cleanup")
            except Exception:
                pass
        raise
    result = json.loads(output.decode().strip().splitlines()[-1])
    print(json.dumps(result, sort_keys=True))
    if result.get("insufficient_space"):
        sys.exit(1)


if __name__ == "__main__":
    main()
