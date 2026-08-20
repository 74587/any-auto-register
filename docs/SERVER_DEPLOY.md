# 无头服务器部署

面向"只跑 Web UI + ChatGPT/iCloud 注册"的 Linux 服务器，配套
`Dockerfile.server` 与 `docker-compose.server.yml`。

需要 Turnstile Solver 或有头浏览器时请改用根目录的 `Dockerfile` /
`docker-compose.yml`，那套会额外装 Playwright 浏览器、Camoufox 和 Go。

## 为什么单独一套

ChatGPT 注册已经整体切到 `platforms/chatgpt/protocol` 的纯协议实现，iCloud 声明
`supported_executors = ["protocol"]`，两个平台都不碰浏览器。唯一用 Playwright 的
`payment.open_url_incognito` 是"在本机弹个付款页"的桌面便利功能，无头服务器上没有
意义，缺浏览器时它会自行回退。砍掉浏览器栈后镜像从约 5GB 降到约 1.1GB，在 2 核
2G 的小机器上构建也只要一两分钟。

**Node 是运行时硬依赖**，镜像里已经装好。ChatGPT 的 Sentinel PoW 必须在 Node 沙箱
里跑 OpenAI 的 `sdk.js`；缺它算出来的 token 过不了服务端复核，验证码邮件会被静默
丢弃——链路看着一切正常但码永远收不到。

## 部署

```bash
mkdir -p /opt/any-auto-register/{data,src}
# 把项目代码放到 /opt/any-auto-register/src
cd /opt/any-auto-register/src

cat > .env <<'EOF'
# 只绑 docker 网桥网关：反向代理容器能到，公网到不了
APP_PORT_BIND=172.17.0.1:8000
# 运行时数据放在 src 之外，重新部署时不会被覆盖
APP_RUNTIME_BIND=/opt/any-auto-register/data
EOF

docker compose -f docker-compose.server.yml up -d --build
```

`APP_RUNTIME_BIND` 指向的目录是唯一的持久化位置（SQLite 库和日志都在里面）。
**部署前就要定好**：换这个路径等于换一个空库，已保存的登录密码和配置都会看不见。

## 反向代理

容器只监听 `172.17.0.1:8000`，公网访问不到，需要由宿主机的 nginx 转发。
任务日志走 SSE，`/api/tasks/` 必须关掉 nginx 的响应缓冲，否则前端看不到实时日志：

```nginx
server {
    listen 80;
    server_name reg.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name reg.example.com;

    ssl_certificate     /etc/nginx/ssl/your.pem;
    ssl_certificate_key /etc/nginx/ssl/your.key;

    client_max_body_size 32m;

    location /api/tasks/ {
        proxy_pass http://172.17.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding off;
        proxy_read_timeout 3600s;
    }

    location / {
        proxy_pass http://172.17.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $http_connection;
        proxy_read_timeout 300s;
    }
}
```

## ⚠️ 必须先设登录密码

没设密码时 `main.py` 的鉴权中间件会直接放行所有 `/api/` 请求：

```python
if not _cs.get("auth_password_hash", ""):
    return await call_next(request)
```

这个面板管着账号、Token、接码 API Key、邮箱和代理凭据。**对外暴露之前**先在
「全局配置 → 安全」里设密码，或者：

```bash
curl -X POST http://172.17.0.1:8000/api/auth/setup \
     -H 'Content-Type: application/json' -d '{"password":"你的强密码"}'
```

设完确认一下无 token 会被挡：

```bash
curl -o /dev/null -w '%{http_code}\n' http://172.17.0.1:8000/api/config   # 期望 401
```

## 更新

```bash
cd /opt/any-auto-register/src
docker compose -f docker-compose.server.yml up -d --build
```

`data/` 不在 `src/` 里，重新部署不会动到数据。

## 自检

```bash
# 平台是否加载
curl -s http://172.17.0.1:8000/api/platforms

# Sentinel PoW 能不能真的算出来（缺 node 或被墙都会在这里暴露）
docker exec -e PYTHONPATH=/app -w /app any-auto-register python - <<'EOF'
from platforms.chatgpt.protocol import sentinel_quickjs as sq
from platforms.chatgpt.protocol.http_client import create_http_session
r = sq.get_sentinel_token_via_quickjs(
    create_http_session(impersonate="chrome131"),
    "deadbeef-0000-4000-8000-000000000001",
    flow="oauth_create_account", timeout_ms=60000)
print("OK" if r else "FAILED", len(r[0]) if r else "")
EOF
```
