// deploy_render.mjs —— 用 Render API 把 st-shim 部署成一个免费 Web 服务
//
// 用法（在有 Node 的机器上）：
//   node deploy_render.mjs --repo https://github.com/你的账号/st-shim \
//        --key rnd_xxxx --token 控制台口令 [--name st-shim] [--region oregon]
//
// 它会：建服务 → 等构建完成 → 打印你的接口地址和 Tavo 该填什么。
// 免费套餐的实例在 15 分钟无请求后会休眠，下次请求需要约 50 秒唤醒。

const args = {};
for (let i = 2; i < process.argv.length; i += 2) {
  args[process.argv[i].replace(/^--/, '')] = process.argv[i + 1];
}
const REPO = args.repo, KEY = args.key, ADMIN = args.token;
const NAME = args.name || 'st-shim';
const REGION = args.region || 'oregon';
const BRANCH = args.branch || 'main';
const OWNER_FALLBACK = args.owner || '';

if (!REPO || !KEY || !ADMIN) {
  console.error('缺少参数。示例：\n  node deploy_render.mjs --repo https://github.com/you/st-shim --key rnd_xxx --token 你的控制台口令');
  process.exit(1);
}

const BASE = 'https://api.render.com/v1';
const H = { Authorization: 'Bearer ' + KEY, Accept: 'application/json', 'Content-Type': 'application/json' };

async function api(method, path, body) {
  const r = await fetch(BASE + path, { method, headers: H, body: body ? JSON.stringify(body) : undefined });
  const t = await r.text();
  if (!r.ok && r.status !== 204) throw new Error(`${method} ${path} -> ${r.status} ${t.slice(0, 300)}`);
  return t ? JSON.parse(t) : null;
}

const owners = await api('GET', '/owners?limit=20');
const owner = OWNER_FALLBACK || owners[0]?.owner?.id;
console.log('账号 ownerId =', owner, '|', owners[0]?.owner?.name || '');

const created = await api('POST', '/services', {
  type: 'web_service',
  name: NAME,
  ownerId: owner,
  repo: REPO,
  branch: BRANCH,
  autoDeploy: 'yes',
  serviceDetails: {
    env: 'python',
    plan: 'free',
    region: REGION,
    healthCheckPath: '/healthz',
    envSpecificDetails: {
      buildCommand: 'python -m py_compile st_shim.py st_shim_web.py',
      startCommand: 'python st_shim_web.py',
    },
  },
});
const sid = created.service.id;
const url = created.service.serviceDetails?.url || '';
console.log('\n服务已创建：');
console.log('  名称   ', created.service.name);
console.log('  id     ', sid);
console.log('  地址   ', url);

// 写入控制台口令
await api('PUT', `/services/${sid}/env-vars`, [
  { key: 'ADMIN_TOKEN', value: ADMIN },
]);
console.log('\n已写入环境变量 ADMIN_TOKEN（控制台口令）');

// 等第一次构建
console.log('\n等待首次构建完成（最多 10 分钟）…');
const deadline = Date.now() + 10 * 60 * 1000;
let last = '';
while (Date.now() < deadline) {
  await new Promise(r => setTimeout(r, 15000));
  const deploys = await api('GET', `/services/${sid}/deploys?limit=1`);
  const d = deploys[0]?.deploy;
  if (!d) continue;
  if (d.status !== last) { last = d.status; console.log('  构建状态：' + d.status); }
  if (['live', 'build_failed', 'canceled', 'deploy_failed'].includes(d.status)) break;
}

const final = await api('GET', `/services/${sid}/deploys?limit=1`);
const status = final[0]?.deploy?.status;
console.log('\n最终状态：' + status);

if (status === 'live') {
  console.log(`
================= Tavo 这样填 =================
接口地址 Base URL : ${url}/v1
API Key          : 你在控制台给站点设的「客户端令牌」（没设就填上游 key）
模型名            : 和上游一致

控制台（加站点/切站点/测试）：
  ${url}/?token=${ADMIN}

提醒：免费实例 15 分钟无请求会休眠，下次请求约 50 秒唤醒。
      想一直醒着，用 UptimeRobot 之类的免费监控每 5 分钟打一次 ${url}/healthz
==============================================`);
} else {
  console.log('\n构建没有成功。去 ' + (created.service.dashboardUrl || 'Render 控制台') + ' 看构建日志。');
}
