# 把看板挂到公网

三步。假设代码在 `/home/ubuntu/workspace/unmanned-factory`，数据在
`/home/ubuntu/factory-data`。

## 1. 准备数据目录

```sh
mkdir -p ~/factory-data/queue
cd ~/factory-data/queue && mkdir -p inbox running done needs-human blocked log
```

队列子目录必须齐全。缺了的话看板能打开，但一提需求就报「不像一个队列」——
`--queue` 指向的目录不带这些子目录时，依赖树会退化成平表，网页提需求直接失败。

## 2. 起服务

```sh
sudo cp deploy/factory-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now factory-dashboard
systemctl is-active factory-dashboard   # 应该输出 active
```

看板绑 `127.0.0.1:8788`，此时还连不上公网 —— 这是对的。

## 3. 配反代 + 认证

```sh
caddy hash-password --plaintext '你的密码'    # 拿到 $2a$14$... 哈希
```

把 `deploy/Caddyfile.example` 里的 `HOST` 和哈希替换掉，然后：

```sh
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
```

先 validate 再 reload。配置写坏时 reload 会让整个 Caddy 起不来，
连带打挂同一台机器上的其他站点。

## 验一遍

```sh
curl -s -o /dev/null -w '%{http_code}\n' http://HOST/                    # 401
curl -s -o /dev/null -w '%{http_code}\n' -u admin:密码 http://HOST/      # 200
```

再用浏览器提一个需求，确认表单真的有反应 —— 这条链路上踩过两个坑
（表单绝对路径、Origin 白名单枚举本机名），两个的症状都是「点了没反应」
而不是报错。现在有测试兜着，但换部署形态时值得手验一次。

## 为什么认证在 Caddy 层

dashboard 自带的 token + Origin 检查只防 CSRF，**GET 全部无认证**。
审计库里有 diff 和模型原始输出，裸在公网上等于公开代码和成本数据。
所以 basic_auth 是必需的。

dashboard 刻意不提供 `--host` 参数，固定绑 loopback —— 暴露面只由
Caddy 配置决定，不会因为漏传一个参数就意外全网可达。

## 提需求需要 worker 二进制

网页提需求会调 `--binary` 指定的 CLI（默认 `claude`）做需求提取。
那个二进制不在 PATH 时，表单返回 200 但结果里是
`无法启动 claude: [Errno 2] No such file or directory`。
只看审计数据不需要它，要用写入口就得先装。
