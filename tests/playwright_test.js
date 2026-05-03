const { chromium } = require('playwright');
const { spawn } = require('child_process');

async function run() {
  // Start a simple static server using Python
  const server = spawn('python3', ['-m', 'http.server', '8000'], { cwd: process.cwd(), stdio: ['ignore', 'pipe', 'pipe'] });

  server.stdout.on('data', (d) => process.stdout.write(`[http-server] ${d}`));
  server.stderr.on('data', (d) => process.stderr.write(`[http-server] ${d}`));

  // Give server a moment to start
  await new Promise((r) => setTimeout(r, 800));

  const browser = await chromium.launch({ headless: true, args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] });
  const context = await browser.newContext();

  try {
    // Grant camera permission for the test origin
    await context.grantPermissions(['camera'], { origin: 'http://localhost:8000' });

    const page = await context.newPage();
    console.log('Opening test page...');
    await page.goto('http://localhost:8000/test_qr.html', { waitUntil: 'domcontentloaded' });

    // Click start button
    await page.click('#btn-start');

    // Wait for video.srcObject to be set with active tracks
    const ok = await page.waitForFunction(() => {
      const v = document.getElementById('qr-video');
      if (!v) return false;
      const s = v.srcObject;
      return !!(s && s.getTracks && s.getTracks().length > 0);
    }, { timeout: 5000 }).catch(() => null);

    if (ok) {
      console.log('✅ Camera stream active (video.srcObject has tracks)');
    } else {
      console.error('❌ Camera stream NOT active within timeout');
      // Dump navigator.mediaDevices info
      const info = await page.evaluate(() => ({
        hasMediaDevices: !!navigator.mediaDevices,
        getUserMediaAvailable: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
      }));
      console.log('navigator.mediaDevices info:', info);
    }

  } finally {
    await browser.close();
    server.kill();
  }
}

run().catch((e) => { console.error(e); process.exit(1); });
