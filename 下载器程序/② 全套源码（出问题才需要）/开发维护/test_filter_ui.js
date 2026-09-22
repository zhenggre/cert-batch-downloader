// 筛选面板「真机」回归测试 —— 用真实的 Edge 手点一遍，不是模拟。
//
// 【为什么非要有它】
//   2026-09-21 用户验收时报「勾选，选不上」。根因是 fPaint() 里写的
//   `fcond[col] || 新建临时对象` —— 没先在输入框打过字的列，勾上的值写进临时对象、
//   下一次重画就被丢掉。而当时所有测试都走「先打字、再勾选」的路径
//   （打字会顺手把 fcond[col] 建出来），恰好绕开了这个 bug；
//   mini-DOM 模拟也验不出浏览器原生的复选框行为。
//   → 这个测试必须**开真浏览器点**，专门覆盖「打开下拉 → 直接点勾」这条最短路径。
//
// 【怎么跑】
//   1. 先启动 ① 里的 证书下载器.exe（或源码版服务），它会写出一份 运行中.json
//   2. NODE_PATH=<装着 playwright-core 的 node_modules> node test_filter_ui.js
//      本机是 （本机 WorkBuddy 路径）\binaries\node\workspace\node_modules
//   3. 用的就是系统里的 Edge，不用另外下浏览器内核
//
// 【它验什么】
//   A 筛选卡默认可见 / B 筛选在名单与下载按钮之间 / C 读名单 / D **展开▼后勾选能否勾住** /
//   E 同列多勾 / F 打字只缩选项不动数据 / G 清空 / H 全选 / I 跨列联动 / J 有无 JS 报错
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');
const os = require('os');

// 默认测「② 源码版」起的服务（改代码后先跑源码版最省事）。
// 想测打包好的 exe，就设环境变量 CERTDL_RUN 指向 ① 里的 运行中.json。
const RUN = process.env.CERTDL_RUN ||
  path.resolve(__dirname, '..', '程序', '运行中.json');
// 测试名单必须由环境变量指定（姓名 + 证件号两列的 xlsx）。
// 真实名单不进仓库，也不写死在本文件里。
const LIST = process.env.CERTDL_TEST_LIST || '';
if (!LIST) {
  console.error('缺少测试名单。用法：CERTDL_TEST_LIST=/path/to/list.xlsx node test_filter_ui.js');
  process.exit(1);
}
// 截图落到系统临时目录，别堆在项目里（会被 git 收走）
const SHOT_DIR = process.env.CERTDL_SHOT || path.join(os.tmpdir(), 'certdl_shots');
fs.mkdirSync(SHOT_DIR, { recursive: true });
const SHOT = SHOT_DIR + path.sep;

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log('  ✅ ' + name + (extra ? '  ' + extra : '')); }
  else { fail++; console.log('  ❌ ' + name + (extra ? '  ' + extra : '')); }
}

(async () => {
  const run = JSON.parse(fs.readFileSync(RUN, 'utf8'));
  const URL = 'http://127.0.0.1:' + run.port + '/?token=' + run.token;

  const b = await chromium.launch({
    executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true,
  });
  const page = await b.newPage({ viewport: { width: 1280, height: 1400 } });
  const errs = [];
  // favicon 的 403 是浏览器自动请求 /favicon.ico（不带 token）造成的，
  // 程序本身固有行为，跟页面代码无关 —— 别让它把测试判成失败。
  page.on('pageerror', e => errs.push(e.message));
  page.on('console', m => {
    if (m.type() !== 'error') return;
    const t = m.text();
    if (/favicon|Failed to load resource|403/i.test(t)) return;
    errs.push('console: ' + t);
  });

  await page.goto(URL, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(1500);

  console.log('【A】页面一进来，筛选区是否已经露脸');
  const cardVisible = await page.locator('#filter-card').isVisible();
  ok('筛选卡可见', cardVisible);
  const note0 = (await page.locator('#f-note').textContent()).trim();
  ok('有引导语（不是空白）', note0.length > 10, '「' + note0.slice(0, 28) + '…」');
  await page.screenshot({ path: SHOT + 'shot_1_初始.png', fullPage: true });

  console.log('\n【B】位置：筛选是不是夹在「名单」和「开始下载」中间');
  const order = await page.evaluate(() => {
    const y = id => { const e = document.getElementById(id); return e ? e.getBoundingClientRect().top : -1; };
    const startBtn = document.getElementById('btn-start').getBoundingClientRect().top;
    const listRow = document.getElementById('drop').getBoundingClientRect().top;
    return { list: listRow, filter: y('filter-card'), start: startBtn };
  });
  ok('筛选在名单下方', order.filter > order.list, '名单y=' + Math.round(order.list) + ' 筛选y=' + Math.round(order.filter));
  ok('筛选在「开始下载」上方', order.filter < order.start, '筛选y=' + Math.round(order.filter) + ' 按钮y=' + Math.round(order.start));

  console.log('\n【C】拖名单进去');
  await page.setInputFiles('#file', LIST);
  await page.waitForFunction(() => document.querySelectorAll('#f-cols .fcol').length > 3, null, { timeout: 60000 });
  const ncol = await page.locator('#f-cols .fcol').count();
  const head = (await page.locator('#drop-info').textContent()).trim().split('\n')[0];
  ok('识别出名单', ncol > 3, ncol + ' 个可筛列 | ' + head);
  await page.screenshot({ path: SHOT + 'shot_2_名单已读.png', fullPage: true });

  // 找「奖项」列
  const idx = await page.evaluate(() =>
    [...document.querySelectorAll('#f-cols .fcol .fn')].findIndex(e => e.textContent === '奖项'));
  ok('找到「奖项」列', idx >= 0, '第 ' + idx + ' 列');
  const box = page.locator('#f-cols .fcol').nth(idx);

  console.log('\n【D】展开 ▼ 并勾第一个选项 —— 这一步就是「勾不上」的现场');
  ok('展开前列表不可见', !(await box.locator('.flist').isVisible()));
  await box.locator('.fcol-top').click();
  await page.waitForTimeout(250);
  ok('点 ▼ 后展开', await box.locator('.flist').isVisible());

  const optN = await box.locator('.flist label').count();
  const firstVal = (await box.locator('.flist label em').first().textContent()).trim();
  ok('列出了选项', optN > 0, optN + ' 个，第一个是「' + firstVal + '」');

  await box.locator('.flist label input').first().click();   // ← 真鼠标点
  await page.waitForTimeout(300);

  const checkedN = await box.locator('.flist label input:checked').count();
  ok('勾上了（勾选状态没丢）', checkedN === 1, '勾中 ' + checkedN + ' 项');
  const leftTxt = (await page.locator('#f-left').textContent()).trim();
  ok('右上角人数变了', leftTxt.includes('筛完'), '「' + leftTxt + '」');
  const chipTxt = (await page.locator('#f-chips').textContent()).trim();
  ok('出现了条件标签', chipTxt.includes('奖项'), '「' + chipTxt + '」');
  const firstNow = (await box.locator('.flist label em').first().textContent()).trim();
  ok('勾中的项排在列表最前', firstNow === firstVal, '第一项 =「' + firstNow + '」');
  await page.screenshot({ path: SHOT + 'shot_3_勾选1项.png', fullPage: true });

  console.log('\n【E】再勾一个（同列多勾 = 或）');
  await box.locator('.flist label input').nth(1).click();
  await page.waitForTimeout(300);
  const checkedN2 = await box.locator('.flist label input:checked').count();
  ok('两项都保持勾住', checkedN2 === 2, '勾中 ' + checkedN2 + ' 项');
  const leftTxt2 = (await page.locator('#f-left').textContent()).trim();
  ok('人数随勾选变化', leftTxt2.includes('筛完'), '「' + leftTxt2 + '」');
  await page.screenshot({ path: SHOT + 'shot_4_勾选2项.png', fullPage: true });

  console.log('\n【F】在框里打字 = 只筛选项、不动数据');
  const kw = box.locator('.fkw');
  await kw.fill('一等');
  await page.waitForTimeout(300);
  const optN2 = await box.locator('.flist label').count();
  const leftTxt3 = (await page.locator('#f-left').textContent()).trim();
  ok('选项被搜窄了', optN2 <= optN, optN + ' → ' + optN2 + ' 项');
  ok('人数没被打字改动', leftTxt3 === leftTxt2, '「' + leftTxt3 + '」');
  ok('勾住的项没被搜索刷掉', (await box.locator('.flist label input:checked').count()) === 2);

  console.log('\n【G】「清空」按钮');
  await kw.fill('');
  await page.waitForTimeout(200);
  await box.locator('.ftools button', { hasText: '清空' }).click();
  await page.waitForTimeout(300);
  ok('清空后没有勾选', (await box.locator('.flist label input:checked').count()) === 0);
  ok('人数回到未筛选', (await page.locator('#f-left').textContent()).includes('未筛选'));

  console.log('\n【H】「全选」按钮');
  await box.locator('.ftools button', { hasText: '全选' }).click();
  await page.waitForTimeout(400);
  const checkedAll = await box.locator('.flist label input:checked').count();
  ok('全选勾上了', checkedAll > 0, checkedAll + ' 项');
  await page.locator('#f-reset').click();
  await page.waitForTimeout(300);
  ok('「清空全部筛选」生效', (await page.locator('#f-left').textContent()).includes('未筛选'));

  console.log('\n【I】联动：筛一列，另一列的选项跟着变少');
  const idxCity = await page.evaluate(() =>
    [...document.querySelectorAll('#f-cols .fcol .fn')].findIndex(e => e.textContent.includes('所在地区(市)')));
  const cityBox = page.locator('#f-cols .fcol').nth(idxCity);
  await cityBox.locator('.fcol-top').click();
  await page.waitForTimeout(200);
  const cityOptsBefore = await cityBox.locator('.flist label').count();
  await cityBox.locator('.flist label input').first().click();
  await page.waitForTimeout(300);
  const awardOptsAfter = await box.locator('.flist label').count();
  ok('另一列选项被联动收窄', awardOptsAfter <= optN, '奖项列 ' + optN + ' → ' + awardOptsAfter + ' 项');

  console.log('\n【J】出错与异常');
  ok('页面无 JS 报错', errs.length === 0, errs.length ? errs.slice(0, 3).join(' | ') : '');

  await page.screenshot({ path: SHOT + 'shot_5_最终.png', fullPage: true });
  await b.close();

  console.log('\n' + '='.repeat(52));
  console.log('通过 ' + pass + ' 项，失败 ' + fail + ' 项');
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('脚本炸了:', e.message); process.exit(2); });
