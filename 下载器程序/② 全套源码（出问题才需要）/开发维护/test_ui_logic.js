// 用迷你 DOM 真跑一遍页面脚本里的关键函数。
// 语法检查只能证明"能解析"，证明不了"点了会怎样"。
// 用法（需要 node）：node 开发维护/test_ui_logic.js
const fs = require('fs');
const vm = require('vm');
const path = require('path');

// 界面文件在「程序/web/」下 —— 目录重组后位置变了，别写死成 ..\web
const HTML = path.join(__dirname, '..', '程序', 'web', 'index.html');
const src = fs.readFileSync(HTML, 'utf8');
let js = src.match(/<script>([\s\S]*?)<\/script>/)[1];
js = js.replace('__TOKEN__', 'deadbeef');
// init() 会发网络请求，这里不跑它；后面的断言手动调函数
js = js.replace(/\ninit\(\);\s*$/, '\n');

const made = {};
function makeEl(id) {
  return {
    id, value: '', checked: false, textContent: '', innerHTML: '', disabled: false,
    style: {}, dataset: {}, className: '', scrollTop: 0, scrollHeight: 0,
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
                 contains(c) { return this._s.has(c); } },
    appendChild() {}, addEventListener() {}, files: [], onclick: null, onchange: null,
    oninput: null, onkeydown: null, open: false,
  };
}
const elems = new Proxy(made, {
  get(t, k) { if (typeof k !== 'string') return undefined; return t[k] || (t[k] = makeEl(k)); },
});

const sandbox = {
  console,
  document: {
    getElementById: id => elems[id],
    createElement: () => makeEl('tmp'),
    body: makeEl('body'),
  },
  window: { fetch: () => Promise.reject(new Error('no net')), addEventListener() {} },
  btoa: s => Buffer.from(s, 'binary').toString('base64'),
  atob: s => Buffer.from(s, 'base64').toString('binary'),
  setTimeout, clearTimeout, setInterval, clearInterval, Date, Math, JSON, String, Number,
  Promise, Uint8Array, Array, Object, Error, parseInt, isNaN,
};
sandbox.globalThis = sandbox;

let ok = 0, fail = 0;
function check(name, cond, extra) {
  if (cond) { ok++; console.log('  [OK] ' + name); }
  else { fail++; console.log('  [!!] ' + name + '   ' + (extra === undefined ? '' : extra)); }
}

try {
  const ctx = vm.createContext(sandbox);
  vm.runInContext(js + '\n;globalThis.__T = { effOut, updPreview, toB64, esc, guessClass, appendLog };', ctx);
  const T = sandbox.__T;

  console.log('='.repeat(66));
  console.log('① 保存位置预览（同事看的就是这行字）');
  console.log('='.repeat(66));

  const d = new Date();
  const today = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' +
                String(d.getDate()).padStart(2, '0');

  elems['out'].value = 'D:\\证书';
  elems['sub'].checked = true;
  const p1 = T.effOut();
  check('勾选时套上日期文件夹', p1 === 'D:\\证书\\证书_' + today, p1);
  console.log('     预览路径：' + p1);

  elems['sub'].checked = false;
  check('不勾选时原样返回', T.effOut() === 'D:\\证书', T.effOut());

  elems['sub'].checked = true;
  elems['out'].value = 'D:\\证书\\';
  check('输入框末尾带斜杠也不会出双斜杠',
        T.effOut() === 'D:\\证书\\证书_' + today, T.effOut());

  elems['out'].value = '';
  check('没填位置时返回空（提示他去挑一个）', T.effOut() === '', JSON.stringify(T.effOut()));

  console.log();
  console.log('='.repeat(66));
  console.log('② 实时提示文字');
  console.log('='.repeat(66));
  elems['out'].value = 'D:\\证书';
  T.updPreview();
  const h1 = elems['out-preview'].innerHTML;
  check('提示里写明了最终落点', h1.includes('证书会存到'), h1.slice(0, 90));
  check('提示里带上了真实路径', h1.includes('证书_' + today), h1.slice(0, 110));
  check('勾选时解释了"分几次跑会累计"', h1.includes('累计'), h1.slice(0, 160));
  console.log('     ' + h1.replace(/<[^>]+>/g, '').slice(0, 110));

  elems['sub'].checked = false;
  T.updPreview();
  const h2 = elems['out-preview'].innerHTML;
  check('不勾选时改成另一种说法（不能还提累计）',
        h2.includes('放在这个位置下') && !h2.includes('累计'), h2.slice(0, 160));

  elems['out'].value = '';
  T.updPreview();
  check('没选位置时提示去挑，而不是显示一个假路径',
        elems['out-preview'].innerHTML.includes('挑一个'),
        elems['out-preview'].innerHTML.slice(0, 80));

  console.log();
  console.log('='.repeat(66));
  console.log('③ 大文件转换（旧写法有 6 万参数上限，会 RangeError）');
  console.log('='.repeat(66));
  const big = Buffer.alloc(300 * 1024);            // 300KB，远超旧写法的上限
  for (let i = 0; i < big.length; i++) big[i] = i % 251;
  const got = T.toB64(big.buffer.slice(big.byteOffset, big.byteOffset + big.length));
  const want = big.toString('base64');
  check('300KB 能转成功（旧写法在这个尺寸会崩）', got.length === want.length,
        got.length + ' vs ' + want.length);
  check('内容和标准 base64 完全一致', got === want);
  const chunkEdge = T.toB64(new Uint8Array([0, 127, 255, 1, 254]).buffer);
  check('极短内容也对', chunkEdge === Buffer.from([0, 127, 255, 1, 254]).toString('base64'),
        chunkEdge);

  console.log();
  console.log('='.repeat(66));
  console.log('④ HTML 转义（路径里出现 < > 不能把界面搞乱）');
  console.log('='.repeat(66));
  check('< 被转义', T.esc('a<b') === 'a&lt;b', T.esc('a<b'));
  check('& 被转义', T.esc('a&b') === 'a&amp;b', T.esc('a&b'));
  check('引号被转义', T.esc('a"b') === 'a&quot;b', T.esc('a"b'));
  elems['out'].value = 'D:\\<怪>文件夹';
  elems['sub'].checked = false;
  T.updPreview();
  const h3 = elems['out-preview'].innerHTML;
  check('路径里的尖括号进提示后被转义（不会破坏页面结构）',
        !/<怪>/.test(h3) && h3.includes('&lt;怪&gt;'), h3.slice(0, 120));

  console.log();
  console.log('='.repeat(66));
  console.log('⑤ 日志上色规则（能不能一眼看出哪些是"要动手做的事"）');
  console.log('='.repeat(66));
  check('分隔线', T.guessClass('──────────────────────') === 'rule');
  check('【】提示算警告', T.guessClass('【任务提前停止】这不是白干') === 'warn');
  check('带圈的步骤算"要做的事"', T.guessClass('① 退出 VPN') === 'fix');
  check('普通日志不上色', T.guessClass('    查询 20/100') === '');

} catch (e) {
  fail++;
  console.log('  [!!] 脚本执行出错：' + e.message);
  console.log(e.stack);
}

console.log();
console.log('='.repeat(66));
console.log('结果：' + ok + ' 项通过，' + fail + ' 项失败');
console.log('='.repeat(66));
process.exit(fail ? 1 : 0);
