/**
 * 真机测试（Playwright 驱动真 Edge）：新比赛「识别出来的内容，自己改」
 * =====================================================================
 * 背景：自动识别读的是**网页标题**，而同一个表单模板换个比赛接着用时，
 *       标题常常还是上一场留下的 —— 2026-09-21 用户那场国赛被认成
 *       「快递信息填写」；更要命的是证书文件字段被认成 F13，整批下载全失败。
 *       所以：名字要能改，证书文件字段也要能改。
 *
 * 两种模式：
 *   · 在线（默认）：真的去识别 CERTDL_TEST_URL，端到端
 *   · 离线（CERTDL_OFFLINE=1）：用伪造的 /api/probe 响应，只验**界面渲染与传参**
 *     —— 网站不稳时（2026-09-21 当天就抓不到页面）仍然能测到界面这半截；
 *     后端落盘那半截由 test_site_custom.py 覆盖。
 *
 * 安全：结束（含中途失败）一律把 config.json 还原 —— 绝不在用户电脑上留下测试比赛。
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright-core');

const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const RUN = process.env.CERTDL_RUN || path.resolve(__dirname, '..', '程序', '运行中.json');
const TESTURL = process.env.CERTDL_TEST_URL || 'https://biaodan100.com/q/zzzzzz';
const OFFLINE = process.env.CERTDL_OFFLINE === '1';
const NEWNAME = '示例比赛（测试改名）';
const NEWFU = 'URL';        // 故意改掉，验证「证书文件字段」真的能自定义

const run = JSON.parse(fs.readFileSync(RUN, 'utf8'));
const PROG = path.dirname(RUN);
const CFG = path.join(PROG, 'config.json');
const URL = `http://127.0.0.1:${run.port}/?token=${run.token}`;

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log('  OK   ' + name); }
  else { fail++; console.log('  FAIL ' + name + (extra ? '   → ' + extra : '')); }
}

const FAKE_PROBE = {
  ok: true, sid: 'OFFLINE_SID', url: TESTURL,
  title: '快递信息填写',            // 模板残留的标题，正是要改掉的东西
  file_url: 'F13',                  // 认错的证书字段，正是要改掉的东西
  fields: [
    { cn: '姓名', fid: 'F2' }, { cn: '证件号', fid: 'F999' }, { cn: '学校', fid: 'F5' },
    { cn: '奖项', fid: 'F9' }, { cn: '省', fid: 'F10' }, { cn: '市', fid: 'F11' },
    { cn: '区县', fid: 'F1' }, { cn: '指导老师', fid: 'F8' },
  ],
  notes: ['这些字段没被用到（正常，比如「是否入围决赛」）：证书收件人电话=F14、参赛赛项=F6'],
};

(async () => {
  console.log('模式        :', OFFLINE ? '离线（伪造识别响应，验界面与传参）' : '在线（真去识别）');
  console.log('运行中.json :', RUN);
  console.log('待识别网址  :', TESTURL);
  const backup = fs.existsSync(CFG) ? fs.readFileSync(CFG, 'utf8') : null;
  let captured = null;

  const browser = await chromium.launch({ executablePath: EDGE, headless: true });
  const page = await browser.newPage();
  const jsErr = [];
  page.on('pageerror', e => jsErr.push(String(e)));

  if (OFFLINE) {
    await page.route('**/api/probe*', r => r.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify(FAKE_PROBE),
    }));
    await page.route('**/api/site_add*', r => {
      // 把前端**实际发出去**的东西记下来 —— 离线模式要验的就是这个
      try { captured = JSON.parse(r.request().postData() || '{}'); } catch (e) { captured = {}; }
      r.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          ok: true, key: 'site_offline', title: captured.title || '',
          sites: [{ key: 'site_offline', title: captured.title || '' }],
        }),
      });
    });
  }

  try {
    await page.goto(URL, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(900);

    console.log('\n=== A. 打开面板 ===');
    await page.click('#btn-newsite');
    await page.waitForTimeout(300);
    ok('面板打开了', await page.$eval('#newsite', el => el.classList.contains('on')));
    ok('识别之前没有名字框', (await page.$('#ns-title')) === null);

    console.log('\n=== B. 识别 ===');
    await page.fill('#ns-url', TESTURL);
    await page.click('#ns-probe');
    await page.waitForSelector('#ns-title', { timeout: OFFLINE ? 10000 : 150000 });
    const probed = await page.inputValue('#ns-title');
    ok('识别后出现了名字框', true);
    ok('名字框预填了识别结果（不为空）', probed.length > 0, '值=' + probed);
    console.log('     网页标题识别成：「' + probed + '」');
    ok('页面提示了「名字不对就自己改」',
       (await page.$eval('body', el => el.innerText)).includes('名字不对'));
    ok('证书文件字段也露出来了（可核对、可改）', (await page.$('#ns-fileurl')) !== null);
    const probedFu = await page.inputValue('#ns-fileurl');
    ok('证书文件字段有默认值', probedFu.length > 0, '值=' + probedFu);
    console.log('     证书文件字段识别成：「' + probedFu + '」');
    ok('页面提醒了证书字段认错会导致 Invalid URL',
       (await page.$eval('body', el => el.innerText)).includes('Invalid URL'));

    console.log('\n=== C. 名字和证书文件字段都能改 ===');
    await page.fill('#ns-title', NEWNAME);
    ok('名字改成了自定义内容', (await page.inputValue('#ns-title')) === NEWNAME);
    await page.fill('#ns-fileurl', NEWFU);
    ok('证书文件字段也能改', (await page.inputValue('#ns-fileurl')) === NEWFU);

    console.log('\n=== D. 名字留空要拦下来 ===');
    await page.fill('#ns-title', '   ');
    await page.click('#ns-ok');
    await page.waitForTimeout(700);
    const msg = await page.$eval('#ns-result', el => el.innerText);
    ok('留空时提示要先起名字', msg.includes('起个名字'), msg.replace(/\n/g, ' / ').slice(0, 120));
    ok('留空时面板没关（还在等他改）',
       await page.$eval('#newsite', el => el.classList.contains('on')));
    if (!OFFLINE) {
      ok('留空时没有写配置（config.json 一字未动）',
         backup !== null && fs.readFileSync(CFG, 'utf8') === backup);
    } else {
      ok('留空时压根没发添加请求', captured === null);
    }

    console.log('\n=== E. 改名并添加 ===');
    await page.fill('#ns-title', NEWNAME);
    await page.click('#ns-ok');
    await page.waitForTimeout(OFFLINE ? 1200 : 3000);
    if (OFFLINE) {
      ok('提交时把自定义名字发给了后端', (captured || {}).title === NEWNAME,
         JSON.stringify(captured));
      ok('提交时把自定义证书字段发给了后端', (captured || {}).file_url === NEWFU,
         JSON.stringify(captured));
      ok('提交时带上了 sid', !!(captured || {}).sid, JSON.stringify(captured));
    }
    ok('添加后面板自动关掉',
       !(await page.$eval('#newsite', el => el.classList.contains('on'))));
    const selText = await page.$eval('#site', el => el.options[el.selectedIndex].textContent);
    ok('下拉框显示的是**我起的名字**', selText === NEWNAME, '实际显示=' + selText);

    if (!OFFLINE) {
      console.log('\n=== F. 配置核对（真落盘）===');
      const cfg = JSON.parse(fs.readFileSync(CFG, 'utf8'));
      const key = cfg.default_site;
      const s = cfg.sites[key] || {};
      ok('title 是我起的名字', s.title === NEWNAME, '实际=' + s.title);
      ok('站点 key 仍由 shortid 派生（没被名字带偏）',
         typeof key === 'string' && key.startsWith('site_'), 'key=' + key);
      ok('证书文件字段 = 我改的那个', (s.field_map || {}).file_url === NEWFU,
         String((s.field_map || {}).file_url));
      ok('其余字段没被动过（姓名还在）', !!(s.field_map && s.field_map.name),
         JSON.stringify(s.field_map));
    }

    console.log('\n=== G. 页面报错 ===');
    const real = jsErr.filter(e => !/favicon/i.test(e));
    ok('没有 JS 报错', real.length === 0, real.join(' | '));
  } catch (e) {
    fail++;
    console.log('\n  FAIL 测试中断：' + e.message);
  } finally {
    if (backup !== null) {
      fs.writeFileSync(CFG, backup, 'utf8');
      if (!OFFLINE) console.log('\n（config.json 已还原成测试前的样子）');
    }
    await browser.close();
  }

  console.log(`\n=============== 通过 ${pass} / 失败 ${fail} ===============`);
  process.exit(fail ? 1 : 0);
})();
