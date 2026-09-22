// 真机验证：速度加减 / 暂停后改速 / 总进度条 / 跑完回到默认值
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

// 相对本文件定位工具目录：开发维护/../程序
const PROG = path.resolve(__dirname, '..', '程序');
// 默认测「② 源码版」起的服务；想测打包好的 exe，就把 CERTDL_RUN 指向 ① 里的 运行中.json。
// 用户设置.json 跟 运行中.json 同目录，所以直接从 RUN 推导，两种场景都适用。
const RUN = process.env.CERTDL_RUN || path.join(PROG, '运行中.json');
if (!process.env.CERTDL_TEST_LIST) {
  console.error('缺少测试名单。用法：CERTDL_TEST_LIST=/path/to/list.xlsx node test_speed_pause.js');
  process.exit(1);
}
const LIST = process.env.CERTDL_TEST_LIST;
const OUT  = process.env.CERTDL_TEST_OUT ||
  path.join(require('os').tmpdir(), 'certdl_pause_test');
const SETTINGS = path.join(path.dirname(RUN), '用户设置.json');

const run = JSON.parse(fs.readFileSync(RUN, 'utf8'));
const URL = `http://127.0.0.1:${run.port}/?token=${run.token}`;

let pass = 0, fail = 0;
const ck = (n, c, extra = '') => {
  if (c) { pass++; console.log('  OK   ' + n); }
  else { fail++; console.log('  FAIL ' + n + (extra ? '  -> ' + extra : '')); }
};
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const b = await chromium.launch({
    executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true,
  });
  const page = await b.newPage();
  const errs = [];
  page.on('pageerror', e => errs.push('pageerror: ' + String(e.message).split('\n')[0]));
  page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()); });

  const val = () => page.locator('#sp-val').textContent();

  await page.goto(URL, { waitUntil: 'domcontentloaded' });
  await sleep(1500);

  console.log('=== A. 初始状态 ===');
  ck('速度显示默认 1.6 秒/人', (await val()).indexOf('1.6') >= 0, await val());
  ck('有档位提示文字', ((await page.locator('#sp-tip').textContent()) || '').length > 0);
  ck('未运行时「−」可用', !(await page.locator('#sp-minus').isDisabled()));
  ck('未运行时「＋」可用', !(await page.locator('#sp-plus').isDisabled()));

  console.log('=== B. 加减速度 ===');
  await page.locator('#sp-minus').click(); await sleep(250);
  ck('点「−」→ 1.2（更快）', (await val()).indexOf('1.2') >= 0, await val());
  await page.locator('#sp-minus').click(); await sleep(250);
  ck('再点「−」→ 0.9', (await val()).indexOf('0.9') >= 0, await val());
  await page.locator('#sp-plus').click(); await sleep(250);
  ck('点「＋」→ 回到 1.2（更慢）', (await val()).indexOf('1.2') >= 0, await val());
  for (let i = 0; i < 10; i++) { if (await page.locator('#sp-minus').isDisabled()) break; await page.locator('#sp-minus').click(); await sleep(120); }
  ck('一路调到底 = 0.3 且不再能往下', (await val()).indexOf('0.3') >= 0 && await page.locator('#sp-minus').isDisabled(), await val());
  for (let i = 0; i < 12; i++) { if (await page.locator('#sp-plus').isDisabled()) break; await page.locator('#sp-plus').click(); await sleep(120); }
  ck('一路调到顶 = 3.0 且不再能往上', (await val()).indexOf('3.0') >= 0 && await page.locator('#sp-plus').isDisabled(), await val());
  for (let i = 0; i < 12; i++) { if ((await val()).indexOf('1.6') >= 0) break; await page.locator('#sp-minus').click(); await sleep(120); }
  ck('能调回 1.6', (await val()).indexOf('1.6') >= 0, await val());

  console.log('=== C. 改速度不该被写进硬盘（只对当前任务有效）===');
  let st = {};
  try { st = JSON.parse(fs.readFileSync(SETTINGS, 'utf8')); } catch (e) { st = {}; }
  ck('用户设置.json 里没有 speed（没被记住）', !('speed' in st), JSON.stringify(st));

  console.log('=== D. 跑起来后速度被锁住 ===');
  await page.setInputFiles('#file', LIST);
  await sleep(2500);
  const info = (await page.locator('#drop-info').textContent()) || '';
  ck('名单已读取', /人/.test(info), info.trim().slice(0, 40));
  await page.locator('#try').check().catch(() => {});
  await page.fill('#tryn', '8');
  await page.fill('#out', OUT);
  await page.locator('#btn-start').click();

  // 从任务一启动就开始采样进度条，把中间过程全记下来
  const widths = [], stages = [];
  const sampler = setInterval(async () => {
    try {
      widths.push(parseFloat((await page.locator('#bar').evaluate(e => e.style.width)) || '0') || 0);
      stages.push(((await page.locator('#stage-num').textContent()) || '').trim());
    } catch (e) {}
  }, 200);

  await sleep(2500);
  ck('任务已启动（暂停键可用）', !(await page.locator('#btn-pause').isDisabled()));
  ck('运行时「−」被禁用', await page.locator('#sp-minus').isDisabled());
  ck('运行时「＋」被禁用', await page.locator('#sp-plus').isDisabled());
  const lockedTip = (await page.locator('#sp-tip').textContent()) || '';
  ck('提示「想改先点暂停」', lockedTip.indexOf('暂停') >= 0, lockedTip);
  const beforeClick = await val();
  await page.locator('#sp-minus').click({ force: true }).catch(() => {});
  await sleep(300);
  ck('运行时点「−」无效（值没变）', (await val()) === beforeClick, beforeClick + ' -> ' + await val());

  console.log('=== E. 暂停 → 改速度 → 继续 ===');
  await page.locator('#btn-pause').click();
  await sleep(1200);
  ck('暂停键变成「继续」', ((await page.locator('#btn-pause').textContent()) || '').indexOf('继续') >= 0,
     await page.locator('#btn-pause').textContent());
  ck('暂停后「−」解锁', !(await page.locator('#sp-minus').isDisabled()));
  const before = await val();
  await page.locator('#sp-minus').click(); await sleep(400);
  ck('暂停时能改速度', (await val()) !== before, before + ' -> ' + await val());
  const pctAtPause = widths.length ? widths[widths.length - 1] : 0;
  await sleep(1500);
  ck('暂停期间进度条不动', Math.abs((widths[widths.length - 1] || 0) - pctAtPause) < 0.01,
     pctAtPause + ' -> ' + widths[widths.length - 1]);

  await page.locator('#btn-pause').click();   // 继续
  await sleep(2000);
  ck('恢复后按钮回到「暂停」', ((await page.locator('#btn-pause').textContent()) || '').indexOf('暂停') >= 0,
     await page.locator('#btn-pause').textContent());
  const logTxt = (await page.locator('#log').textContent()) || '';
  ck('日志写明速度已换挡', logTxt.indexOf('速度已换成') >= 0);

  for (let i = 0; i < 120; i++) {
    if (await page.locator('#btn-pause').isDisabled()) break;
    await sleep(1000);
  }
  // 任务结束到"进图 100%"之间有不到一帧的缝：采样器有可能正好错过，于是
  // 报一个"最后没到 100%"的**假失败**（2026-09-21 真偶发过一次，重跑就过）。
  // 所以收采样前，先给进度条一点时间把最后一帧走到位 —— 别让测试靠运气。
  for (let i = 0; i < 20; i++) {
    if (Math.max(...(widths.length ? widths : [0])) >= 99.5) break;
    await sleep(300);
  }
  clearInterval(sampler);
  await sleep(600);

  console.log('=== F. 总进度条（这次采到中间过程了）===');
  let mono = true, prev = -1;
  widths.forEach(w => { if (w < prev - 0.01) mono = false; prev = w; });
  const uniq = [...new Set(widths.map(w => Math.round(w)))];
  console.log('     采样宽度: ' + widths.map(w => w.toFixed(0)).join(','));
  ck('进度条只往右走（不回退）', mono);
  ck('进度条是连续推进的（≥4 个不同值，不是每阶段重走）', uniq.length >= 4, '不同取值: ' + uniq.join(','));
  const withTotal = stages.filter(s => s.indexOf('总') >= 0);
  ck('阶段行显示了总百分比', withTotal.length > 0, stages.slice(-3).join(' | '));
  ck('最后的进度到 100%', Math.max(...widths) >= 99.5, String(Math.max(...widths)));

  console.log('=== G. 跑完回到默认速度 ===');
  ck('任务已结束', await page.locator('#btn-pause').isDisabled());
  await sleep(1500);
  ck('速度回到默认 1.6（没被记住）', (await val()).indexOf('1.6') >= 0, await val());
  let st2 = {};
  try { st2 = JSON.parse(fs.readFileSync(SETTINGS, 'utf8')); } catch (e) { st2 = {}; }
  ck('用户设置.json 仍然没有 speed', !('speed' in st2), JSON.stringify(st2));

  console.log('=== H. 控制台 ===');
  const real = errs.filter(e => !/favicon|Failed to load resource|403/i.test(e));
  ck('页面无 JS 报错', real.length === 0, real.slice(0, 3).join(' | '));

  const shotDir = path.join(require('os').tmpdir(), 'certdl_shots');
  fs.mkdirSync(shotDir, { recursive: true });
  await page.screenshot({ path: path.join(shotDir, 'speed_pause.png'), fullPage: false });
  await b.close();
  console.log('\n================ 通过 ' + pass + ' / 失败 ' + fail + ' ================');
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('崩了:', e.message); process.exit(2); });
