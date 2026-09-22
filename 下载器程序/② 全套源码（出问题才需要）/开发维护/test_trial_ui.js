// 真机验证：试跑必须「自报家门」
//
// 2026-09-22 用户筛了 604 人却只跑了 8 个，而报告上写着「全部完成」——
// 根因是「先随机试跑」**默认就是勾着的**，而且跑完没有任何地方说明"这是试跑"。
//
// 这个测试用真 Edge 点一遍，专盯四件事：
//   A 默认必须是关的（选了多少人就得跑多少人）
//   B 一勾上就要立刻亮提示，写明"不是全部人"
//   C 真跑一次：运行中写「试跑中」，跑完写「试跑结束」+ 说明条（只抽查 N 人 / 名单共 M 人）
//   D 全程不许有 JS 报错
//
// 前端交互一律用真浏览器验 —— 模拟环境验不出复选框的初始状态、也验不出
// "标题到底换没换"（那正是这次事故的核心）。
//
// 用法（CERTDL_RUN 指向 ① 里正在运行的 运行中.json）：
//   set CERTDL_RUN=…\① …\证书下载器\运行中.json
//   node test_trial_ui.js
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const PROG = path.resolve(__dirname, '..', '程序');
const RUN = process.env.CERTDL_RUN || path.join(PROG, '运行中.json');
if (!process.env.CERTDL_TEST_LIST) {
  console.error('缺少测试名单。用法：CERTDL_TEST_LIST=/path/to/list.xlsx node test_trial_ui.js');
  process.exit(1);
}
const LIST = process.env.CERTDL_TEST_LIST;
const OUT = process.env.CERTDL_TEST_OUT ||
  path.join(require('os').tmpdir(), 'certdl_trial_test');
const N = parseInt(process.env.CERTDL_TRIAL_N || '3', 10);   // 试跑抽几个人

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

  await page.goto(URL, { waitUntil: 'domcontentloaded' });
  await sleep(1500);

  console.log('=== A. 默认状态：试跑默认是关的 ===');
  ck('「先随机试跑」默认没勾', !(await page.locator('#try').isChecked()));
  ck('默认不显示试跑警告条', !(await page.locator('#try-warn').isVisible()));
  ck('默认的结果卡标题里没有「试跑」字样',
     !((await page.locator('#done-title').textContent()) || '').includes('试跑'));

  console.log('=== B. 一勾上就立刻把话说明白 ===');
  await page.locator('#try').check();
  await sleep(300);
  ck('勾上后警告条出现', await page.locator('#try-warn').isVisible());
  let wtxt = ((await page.locator('#try-warn').textContent()) || '').trim();
  ck('警告条写明了"只会随机查几人"', /\d/.test(wtxt), wtxt);
  ck('警告条写明了"不是全部人"', wtxt.includes('不是全部人'), wtxt);
  await page.fill('#tryn', String(N));
  await sleep(300);
  wtxt = ((await page.locator('#try-warn').textContent()) || '').trim();
  ck('改了人数，警告条跟着变（' + N + '）', wtxt.includes(String(N)), wtxt);
  await page.locator('#try').uncheck();
  await sleep(250);
  ck('取消勾选后警告条消失', !(await page.locator('#try-warn').isVisible()));

  console.log('=== C. 真跑一次试跑 ===');
  await page.setInputFiles('#file', LIST);
  await sleep(2500);
  const info = ((await page.locator('#drop-info').textContent()) || '').trim();
  ck('名单已读取', /人/.test(info), info.slice(0, 40));
  const total = parseInt(((info.match(/(\d[\d,]*)\s*人/) || [])[1] || '0').replace(/,/g, ''), 10);
  ck('读到了名单总人数（后面要拿它核对）', total > N, 'total=' + total + ' ' + info.slice(0, 40));

  await page.locator('#try').check();
  await page.fill('#tryn', String(N));
  await page.fill('#out', OUT);
  await page.locator('#btn-start').click();

  // 运行中：标题必须出现「试跑中」
  let sawTrialRunning = false;
  for (let i = 0; i < 60; i++) {
    const st = ((await page.locator('#stage').textContent()) || '');
    if (st.includes('试跑中')) { sawTrialRunning = true; break; }
    if (await page.locator('#btn-start').isEnabled()) break;      // 已经跑完了
    await sleep(500);
  }
  ck('运行中标题写着「试跑中」', sawTrialRunning, await page.locator('#stage').textContent());

  // 等跑完（有界等待，别死等）
  for (let i = 0; i < 240; i++) {
    if (await page.locator('#btn-start').isEnabled()) break;
    await sleep(1000);
  }
  await sleep(1800);

  const stage = ((await page.locator('#stage').textContent()) || '').trim();
  const title = ((await page.locator('#done-title').textContent()) || '').trim();
  const noteVisible = await page.locator('#trial-note').isVisible();
  const note = ((await page.locator('#trial-note').textContent()) || '').trim();

  ck('跑完的进度标题写「试跑结束」（不再是"已完成"）',
     stage.includes('试跑结束'), stage);
  ck('结果卡标题写「试跑结束」', title.includes('试跑结束'), title);
  ck('结果卡标题写明「这不是全量」', title.includes('不是全量'), title);
  ck('显示了试跑说明条', noteVisible);
  ck('说明条写了「只随机抽查了 ' + N + ' 人」', note.includes('只随机抽查了 ' + N), note.slice(0, 90));
  ck('说明条写出了名单总人数 ' + total, total > 0 && note.includes(String(total)), note.slice(0, 140));
  ck('说明条教了怎么跑全量（去掉勾 + 再点开始）',
     note.includes('去掉') && note.includes('开始'), note.slice(-90));

  console.log('=== D. 页面报错 ===');
  // favicon 的 403 是浏览器自动去要 /favicon.ico（不带 token）造成的，
  // 属于固有行为，跟本次改动无关 —— 别的真机测试也是这么滤的。
  const real = errs.filter(e => !/favicon|Failed to load resource|403/i.test(e));
  ck('没有 JS 报错', real.length === 0, real.slice(0, 3).join(' | '));

  await b.close();
  console.log('\n=============== 通过 ' + pass + ' / 失败 ' + fail + ' ===============');
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('测试中断：' + e); process.exit(2); });
