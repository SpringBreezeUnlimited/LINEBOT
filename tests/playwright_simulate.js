const { chromium } = require('playwright');
const { spawn } = require('child_process');

async function run() {
  const server = spawn('python3', ['-m', 'http.server', '8000'], { cwd: process.cwd(), stdio: ['ignore', 'pipe', 'pipe'] });
  server.stdout.on('data', (d) => process.stdout.write(`[http-server] ${d}`));
  server.stderr.on('data', (d) => process.stderr.write(`[http-server] ${d}`));
  await new Promise((r) => setTimeout(r, 800));

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  try {
    await context.grantPermissions(['camera'], { origin: 'http://localhost:8000' });
    const page = await context.newPage();
    await page.goto('http://localhost:8000/test_qr.html', { waitUntil: 'domcontentloaded' });

    // Inject a fake MediaStream using canvas.captureStream()
    await page.evaluate(() => {
      const v = document.getElementById('qr-video');
      const canvas = document.createElement('canvas');
      canvas.width = 640;
      canvas.height = 480;
      const ctx = canvas.getContext('2d');
      let t = 0;
      function draw() {
        ctx.fillStyle = '#222';
        ctx.fillRect(0,0,canvas.width,canvas.height);
        ctx.fillStyle = 'white';
        ctx.font = '30px sans-serif';
        ctx.fillText('Fake camera frame ' + (t++), 20, 50);
      }
      setInterval(draw, 100);
      const ms = canvas.captureStream(30);
      v.srcObject = ms;
    });

    // Wait a bit and assert that video has tracks
    const ok = await page.waitForFunction(() => {
      const v = document.getElementById('qr-video');
      if (!v) return false;
      const s = v.srcObject;
      return !!(s && s.getTracks && s.getTracks().length > 0);
    }, { timeout: 3000 }).catch(() => null);

    if (ok) console.log('✅ Simulated camera stream attached (video.srcObject has tracks)');
    else console.error('❌ Simulation failed');

  } finally {
    await browser.close();
    server.kill();
  }
}

run().catch(e => { console.error(e); process.exit(1); });
