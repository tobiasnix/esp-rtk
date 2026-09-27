// SPDX-License-Identifier: AGPL-3.0-only
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync('app.js','utf8');
const Model=vm.runInNewContext(source.slice(source.indexOf('class CapacityEstimate'),source.indexOf('const capacityEstimate='))+'\nCapacityEstimate');
const model=new Model();
function sample(at,count,changes={}){model.observe({capacity:{used:1000+count,limit:5000,writable:true},active_line:{id:'trip',state:'recording',auto_distance_m:1,vertex_count:count},...changes},at);}
for(let i=0;i<=60;i+=2)sample(i*1000,i);
assert.strictEqual(model.seconds(600,60000),600,'one point/s gives ten minutes for 600 remaining');
assert.strictEqual(model.seconds(600,80000),null,'stale positions cannot imply remaining recording time');
sample(80000,60);assert.strictEqual(model.seconds(600,80000),null,'a recording gap restarts observation');
for(let i=82;i<=142;i+=2)sample(i*1000,i);
assert(model.seconds(600,142000)>0);
sample(144000,144,{active_line:{id:'trip',state:'paused',auto_distance_m:1,vertex_count:144}});
assert.strictEqual(model.seconds(600,144000),null,'pause suppresses estimate');
for(let i=0;i<=60;i+=2)sample(200000+i*1000,i);
sample(262000,62,{capacity:{used:62,limit:5000,writable:true}});
assert.strictEqual(model.seconds(600,262000),null,'archiving resets history instead of counting freed points');
for(let i=0;i<=60;i+=2)sample(300000+i*1000,i);
sample(362000,1,{active_line:{id:'new-trip',state:'recording',auto_distance_m:1,vertex_count:1}});
assert.strictEqual(model.seconds(600,362000),null,'changing line resets history');
for(let i=0;i<=240;i+=2)sample(400000+i*1000,i);
assert.strictEqual(model.seconds(600,640000),600);
assert(model.samples.length<=61,'only two minutes of observations retained');
sample(642000,240,{capacity:{used:1240,limit:5000,writable:false}});
assert.strictEqual(model.seconds(600,642000),null,'flash reserve prevents a misleading estimate');
sample(700000,0);sample(702000,1);assert.strictEqual(model.seconds(600,702000),null,'short observation is insufficient');
sample(704000,2,{active_line:{id:'trip',state:'recording',auto_distance_m:0,vertex_count:2}});
assert.strictEqual(model.samples.length,0,'manual recording has no automatic time estimate');
console.log('capacity-estimate=ok');
