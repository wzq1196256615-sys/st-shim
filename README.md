# st-shim —— 把客户端请求改造成「酒馆请求」，带多站点网页控制台

**解决的问题**：某些 OpenAI 兼容中转站只服务酒馆（SillyTavern）用户，会用请求头/请求体指纹把非酒馆客户端挡掉。
Tavo、ChatBox、Cherry Studio 这类客户端没法自己伪造指纹，于是：

```
Tavo ──普通请求──> st-shim（本机或云服务器）──酒馆格式请求──> 中转站 /v1
                        ↑ 只改请求头 + 必要的请求体字段，模型回复原样透传
```

**多站点**：网页上随时加/删/切换不同中转站，**Tavo 里那个地址永远不用改**。

---

## 0. 文件清单

| 文件 | 作用 |
| --- | --- |
| `st_shim_web.py` | **推荐入口**：多站点 + 网页控制台（基于下面的核心） |
| `st_shim.py` | 核心：请求头/请求体改写 + 转发（零依赖，纯标准库），也支持单站点直接跑 |
| `st_probe.py` | 诊断：对同一上游分别发「裸请求 / SDK 特征 / 酒馆指纹」，告诉你上游按什么拦人 |
| `mock_site.py` | 离线模拟「只接受酒馆指纹」的站点，用来验证改写是否生效 |
| `run_web.ps1` / `run_local.ps1` | Windows 本地一键启动（多站点版 / 单站点版） |
| `deploy_cloud.sh` | 云服务器一键部署（systemd，多站点版） |
| `Dockerfile` | 用 docker 部署 |
| `profiles.json` | 站点配置（程序自动生成，切站点/改配置都写这里，改完立即生效） |

---

## 1. 先诊断，别瞎试（最重要的一步）

```powershell
python st_probe.py --upstream "https://你买额度那家/v1" --key "sk-你的key" --model "你在那家买的模型名"
```

输出三种请求的结果：

- **A 裸请求被拒、C 酒馆指纹通过** → 站点就是按请求头指纹拦人，直接进第 2 步，默认配置就能过。**最常见。**
- **A/B/C 全部通过** → 拦截点不在请求头，可能在请求体字段。用 `--dump` 看返回原文，再用网页上的「请求体注入字段 / 删除字段」试。
- **A/B/C 全部被拒** → 跟客户端指纹无关（key、余额、模型名、地区限制）。把错误原文发我。

> 说明：伪装用的 `X-ST-Version` / `X-ST-Client` / `X-ST-Chat-Source` 是按国内中转站常见做法加的标记头，
> 不是从酒馆源码抄的。真实站点校验哪几个头，跑一次探针就知道；不够用就在网页「额外请求头」里补，不用改代码。

---

## 2. 本地跑通（30 秒）

```powershell
cd st-shim
.\run_web.ps1
```

- 浏览器打开 **http://127.0.0.1:8787/** → 点「+ 新增站点」→ 填上游地址和 key → **保存** → 点「测试」
- 测试通过后，**Tavo 里填固定地址**：

| Tavo 配置项 | 填什么 |
| --- | --- |
| 接口地址 / Base URL | `http://127.0.0.1:8787/v1` |
| API Key | 该站点设了「客户端令牌」就填它，没设就填上游的 `sk-...` |
| 模型名 | 和上游一致 |

以后要在几个中转站之间换，**只在网页上点「切到这个」**，Tavo 一个字都不用动。
想临时用非当前站点，就把地址里的 `/v1` 换成 `/p/<站点id>/v1`（id 在网页每行显示）。

不想动真钱、想先验证改写逻辑？开三个窗口：

```powershell
python mock_site.py --port 9911     # 窗口1：假的“只收酒馆请求”站点
python mock_site.py --port 9912     # 窗口2：第二个假站点
python st_shim_web.py --port 8787 --upstream http://127.0.0.1:9911/v1 --api-key sk-a --verbose
# 浏览器加两个站点分别指 9911 / 9912，切换后打 /v1，回复里能看到服务端认到了酒馆指纹
```

---

## 3. 上云服务器

```bash
cd st-shim
sudo bash deploy_cloud.sh --admin-token 自己起一个长随机串
```

部署脚本会：装到 `/opt/st-shim`、写 `/etc/st-shim.env`（600 权限）、注册并启动 `systemd` 服务 `st-shim`、健康检查、放行 ufw。
**记得在云服务商安全组放行 8787/tcp。**

然后：

1. 浏览器打开 `http://<云IP>:8787/?token=你的口令`，加站点、填上游、点「测试」。
2. Tavo 里填 `http://<云IP>:8787/v1`（切换站点不用改）。
3. 建议前面挂 Nginx/Caddy 上 HTTPS：控制台 `https://域名/`，接口 `https://域名/v1`。

**安全要点（公网必须做）**：

- `--admin-token` 不设，任何人打开 `http://你的IP:8787/` 就能看到并修改你的站点配置（上游 key 会被他们用）。
- 每个站点建议设「客户端令牌」，并在 Tavo 的 API Key 里填它 —— 这样真 key 只存在服务器上，端口被扫到也白嫖不了。
- 面板只给 `X-Admin-Token` 或 `?token=` 放行；健康检查 `/__stshim/health` 不需要口令。

运维命令：

```bash
tail -f /opt/st-shim/st-shim.log        # 看每个请求走的哪个站点、改成了什么样
systemctl restart st-shim               # 改完 /etc/st-shim.env 后重启
curl -s http://127.0.0.1:8787/__stshim/health
```

Docker：

```bash
docker build -t st-shim .
docker run -d --name st-shim --restart=always -p 8787:8787 \
  -v st-shim-data:/app/data \
  -e ADMIN_TOKEN=你的口令 -e UPSTREAM=https://api.example.com/v1 -e API_KEY=sk-xxx st-shim
```

---

## 4. 控制台能做什么

| 功能 | 说明 |
| --- | --- |
| 加 / 改 / 删站点 | 名称、上游 Base URL、该站点自己的 key、客户端令牌、备注 |
| 一键切换 | 「切到这个」改的是当前启用站点，马上生效，不用重启、不用改 Tavo |
| 一键测试 | 拿酒馆指纹**真打一次上游**：先试 `/models`，再真发一次对话，把上游返回原文摊开给你看 |
| 额外请求头 | 一行一个，形如 `X-Foo: bar`，用来补站点额外校验的头 |
| 请求体注入 / 删除字段 | 一行一个，形如 `chat_completion_source="custom"`；删除字段只写字段名 |
| 中文站点名 | 正常保存（服务端强制按 UTF-8 解析，GBK 客户端也兼容） |

上游 key 不会回传给浏览器：页面只显示「已保存」，留空即表示不修改。

---

## 5. 它到底改了什么

发往上游的请求头（客户端原来的 UA / Origin / Referer / `x-client` / `x-stainless-*` / `openai-*` 等身份头全部丢弃）：

```
Authorization: Bearer <该站点的 key>
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) ... Chrome/124.0.0.0 Safari/537.36
Accept: application/json, text/plain, */*
Accept-Language: zh-CN,zh;q=0.9,en;q=0.8
Content-Type: application/json
Origin: <上游站点根>
Referer: <上游站点根>/
X-Requested-With: XMLHttpRequest
X-ST-Version: 1.13.4
X-ST-Client: SillyTavern
X-ST-Chat-Source: custom
Sec-Fetch-Site: same-origin
Sec-Fetch-Mode: cors
Sec-Fetch-Dest: empty
```

请求体只做最小改动（**prompt、messages、模型名一个字都不动**）：

- 缺失时补 `"stream": false`
- 其余按站点配置注入/删除字段

响应侧：状态码、响应头、响应体（含 SSE 流）原样透传；流式用「发完即断」收尾，客户端能正常读到结尾，不会挂着转圈。
`Origin`/`Referer` 自动跟着**当前站点**走，所以切换站点后同源关系依然正确。

---

## 6. 常用参数

`st_shim_web.py`：

```text
--port          监听端口，默认 8787
--host          监听地址，默认 0.0.0.0（本机自用可改 127.0.0.1）
--admin-token   控制台口令（公网务必设置）
--profiles      站点配置文件路径，默认同目录 profiles.json
--upstream      兼容老用法：首次启动时建一个默认站点
--api-key       配合 --upstream
--client-token  配合 --upstream
--verbose       把改写后的请求头和请求体打进日志
--log           日志文件路径
--insecure      跳过 HTTPS 证书校验（自签证书上游用）
```

单站点版 `st_shim.py` 仍可直接用：`--upstream` `--api-key` `--client-token` `--header` `--json-field` `--strip-field` `--mode tavern|plain` 等。

---

## 7. 排障对照表

| 现象 | 原因 / 处理 |
| --- | --- |
| 控制台打不开 | 服务没起、端口没放行、云安全组没开 8787/tcp |
| 控制台一直提示要口令 | 网址后加 `?token=你的口令`，或在页面顶部输入框填 |
| 页面报 401 | 口令不对 |
| Tavo 报 404 且提示「没有这个站点 id」 | 用了 `/p/<id>/v1`，但 id 写错或站点被删了 |
| Tavo 报 502 且提示「还没填上游地址」 | 去控制台把该站点的上游地址填上 |
| 上游返回 403 / 提示仅支持酒馆 | 先在控制台点「测试」看上游返回原文；不够就在「额外请求头 / 请求体注入字段」补，把原文发我也行 |
| 上游返回 401/402 | 不是指纹问题：key 错或余额不足 |
| 回复能出但 Tavo 一直转圈 | 看 `st-shim.log` 里该请求的 `<- HTTP` 记录，把日志发我 |
| 切了站点没生效 | 确认点了「切到这个」（状态变「启用中」），默认路径只走启用中的站点 |

---

## 8. 边界（说清楚，免得白折腾）

- **能做**：改请求头、补/删请求体字段、多站点切换、prompt 原样送达。指纹类拦截（UA、Origin、Referer、客户端自报头）基本都能过。
- **做不了**：站点若按 **TLS 指纹（JA3）**、**IP 归属地**、或**key 是否绑定酒馆客户端**判断，HTTP 层改不了。
- **风险**：站点这么做是为了筛用户，改指纹属于绕过它的限制，账号被封风险自己评估；公网部署务必设 `--admin-token` 和每站点的「客户端令牌」，别做开放代理。
