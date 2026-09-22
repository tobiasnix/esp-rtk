// SPDX-License-Identifier: AGPL-3.0-only
// Run with Playwright available; optionally set CHROMIUM_EXECUTABLE.
const fs = require('fs'), http = require('http'), assert = require('assert');
const {chromium} = require('playwright');
(async () => {
  const server = http.createServer((req, res) => {
    if (req.url === '/') return res.end('<html></html>');
    if (req.url === '/body') {
      res.writeHead(200, {'Content-Type':'application/json'}); res.flushHeaders();
    }
    setTimeout(() => res.end('{}'), 250);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless:true,
    ...(process.env.CHROMIUM_EXECUTABLE ? {executablePath:process.env.CHROMIUM_EXECUTABLE} : {})});
  try {
    const page = await browser.newPage();
    await page.goto('http://127.0.0.1:' + server.address().port + '/');
    const source = fs.readFileSync('app.js', 'utf8');
    await page.addScriptTag({content:source.slice(source.indexOf('let csrfToken='),
      source.indexOf('\nconst roots=')) + '\nglobalThis.probeAPI=api;'});
    const results = await page.evaluate(async () => {
      const outcomes = [];
      for (const [path, routeSignal] of [['/headers',false],['/headers',true],['/body',true]]) {
        const options = {timeout:50};
        if (routeSignal) options.signal = new AbortController().signal;
        try { await probeAPI.json(path,options); outcomes.push('completed'); }
        catch (error) { outcomes.push(error.name); }
      }
      const controller = new AbortController();
      const first = probeAPI.json('/api/ui/live', {signal:controller.signal,timeout:1000});
      const same = probeAPI.json('/api/ui/live', {signal:controller.signal,timeout:1000});
      const independent = probeAPI.json('/api/ui/live', {signal:new AbortController().signal,timeout:1000});
      outcomes.push(first === same, first !== independent);
      const cancelled = first.catch(error => error.name);
      setTimeout(() => controller.abort(),20);
      outcomes.push(await cancelled);
      outcomes.push(JSON.stringify(await independent));
      return outcomes;
    });
    assert.deepStrictEqual(results,['TimeoutError','TimeoutError','TimeoutError',true,true,'AbortError','{}']);
    console.log('PASS: header/body deadlines, navigation cancellation, independent and shared requests');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => {console.error(error);process.exitCode=1;});
