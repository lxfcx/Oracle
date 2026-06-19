## 📮 联系作者

如需部署帮助、功能定制、问题反馈，请联系作者：

* 👤 Telegram：[@lxfcx6](https://t.me/lxfcx6)
* 💬 联系方式：`@lxfcx6`
* 🧩 项目作者：LXFCX

---

## 🧭 常用命令大全

这里整理项目常用命令，包括：

* 🤖 Telegram 机器人安装 / 更新 / 卸载
* 🌐 Web 面板安装 / 更新 / 卸载
* 📡 探针安装 / 管理
* 🔄 服务启动 / 重启 / 查看状态
* 🧾 Telegram 常用操作命令

---

## 🚀 Telegram 机器人安装

### 菜单安装 / 更新

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/lxfcx/Oracle/main/install.sh)
```

运行后会出现菜单：

```text
1) 安装 / 更新机器人
2) 编辑 BOT_TOKEN 和 ADMIN_IDS
0) 卸载机器人并清理所有数据
3) 退出脚本
```

---

### 一行命令安装

```bash
BOT_TOKEN="你的TG_BOT_TOKEN" ADMIN_IDS="你的TG数字ID" AUTO_INSTALL=1 bash <(curl -fsSL https://raw.githubusercontent.com/lxfcx/Oracle/main/install.sh)
```

---

## 🌐 Web 面板安装

### 安装 / 更新 Web 面板

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/lxfcx/Oracle/main/install_web.sh)
```

Web 面板默认访问地址：

```text
http://服务器公网IP:8899
```

---

## 🔄 更新命令

### 更新机器人 bot.py

```bash
cd /opt/server-monitor-bot

URL="https://raw.githubusercontent.com/lxfcx/Oracle/main/bot.py"

curl -fL "$URL" -o /tmp/bot.py.new && \
python3 -m py_compile /tmp/bot.py.new && \
mv /tmp/bot.py.new /opt/server-monitor-bot/bot.py && \
chmod +x /opt/server-monitor-bot/bot.py && \
systemctl restart server-monitor-bot && \
systemctl status server-monitor-bot --no-pager -l
```

---

### 更新 Web 面板 web_dashboard.py

```bash
cd /opt/server-monitor-bot

URL="https://raw.githubusercontent.com/lxfcx/Oracle/main/web_dashboard.py"

curl -fL "$URL" -o /tmp/web_dashboard.py.new && \
python3 -m py_compile /tmp/web_dashboard.py.new && \
mv /tmp/web_dashboard.py.new /opt/server-monitor-bot/web_dashboard.py && \
chmod +x /opt/server-monitor-bot/web_dashboard.py && \
systemctl restart server-monitor-web && \
systemctl status server-monitor-web --no-pager -l
```

---

### 更新探针 agent.sh

在远程服务器执行：

```bash
systemctl stop server-monitor-agent 2>/dev/null || true
bash <(curl -fsSL https://raw.githubusercontent.com/lxfcx/Oracle/main/agent.sh)
```

推荐从 Telegram 或 Web 的服务器详情页复制完整探针部署命令执行。

---

## 🧹 卸载命令

### 卸载机器人和全部数据

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/lxfcx/Oracle/main/uninstall.sh)
```

也可以运行安装脚本菜单卸载：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/lxfcx/Oracle/main/install.sh)
```

选择：

```text
0) 卸载机器人并清理所有数据
```

然后输入：

```text
YES
```

---

### 只卸载 Web 面板

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/lxfcx/Oracle/main/uninstall_web.sh)
```

---

### 卸载远程探针

在远程服务器执行：

```bash
systemctl disable --now server-monitor-agent 2>/dev/null || true
rm -f /etc/systemd/system/server-monitor-agent.service
rm -rf /opt/server-monitor-agent
systemctl daemon-reload
```

---

## 🔍 服务管理命令

### 查看机器人状态

```bash
systemctl status server-monitor-bot --no-pager -l
```

### 重启机器人

```bash
systemctl restart server-monitor-bot
```

### 查看机器人日志

```bash
journalctl -u server-monitor-bot -n 120 --no-pager
```

### 实时查看机器人日志

```bash
journalctl -u server-monitor-bot -f
```

---

### 查看 Web 面板状态

```bash
systemctl status server-monitor-web --no-pager -l
```

### 重启 Web 面板

```bash
systemctl restart server-monitor-web
```

### 查看 Web 面板日志

```bash
journalctl -u server-monitor-web -n 120 --no-pager
```

### 实时查看 Web 面板日志

```bash
journalctl -u server-monitor-web -f
```

---

### 查看探针状态

```bash
systemctl status server-monitor-agent --no-pager -l
```

### 重启探针

```bash
systemctl restart server-monitor-agent
```

### 查看探针日志

```bash
journalctl -u server-monitor-agent -n 120 --no-pager
```

### 实时查看探针日志

```bash
journalctl -u server-monitor-agent -f
```

---

## 📡 探针端口命令

主控服务器需要放行探针上报端口：

```bash
ufw allow 8765/tcp 2>/dev/null || true
iptables -I INPUT -p tcp --dport 8765 -j ACCEPT 2>/dev/null || true
```

测试主控探针接口：

```bash
curl -s http://127.0.0.1:8765/health
```

正常返回：

```json
{"ok": true, "service": "server-monitor-metrics"}
```

---

## 🌐 Web 面板端口命令

Web 面板默认端口：

```text
8899/tcp
```

放行 Web 面板端口：

```bash
ufw allow 8899/tcp 2>/dev/null || true
iptables -I INPUT -p tcp --dport 8899 -j ACCEPT 2>/dev/null || true
```

测试 Web 面板：

```bash
curl -I http://127.0.0.1:8899
```

---

## 🤖 Telegram 常用命令

### 启用中文菜单

```text
启用命令
```

---

### 查看服务器总览

```text
服务器总览
```

---

### 查看所有服务器

```text
查看服务器
```

---

### 查看本机状态

```text
查看状态
```

---

### 添加服务器

```text
添加服务器
```

---

### 查看事件

```text
查看事件
```

---

### 检测服务器

```text
检测服务器
```

---

### 刷新本机地区

```text
刷新本机地区
```

---

### 刷新全部服务器地区

```text
刷新全部地区
```

---

## 🧾 添加服务器示例

```text
添加服务器
名称: HK-Oracle
主机: 1.2.3.4
备注: 香港甲骨文 免费机器
系统: Ubuntu 22.04
周期: 年付
价格: 0
币种: USD
到期: 2026-08-01
检测端口: 22
自动续费: 是
```

---

## 🎁 添加永久免费服务器

```text
添加服务器
名称: Oracle-Free
主机: 1.2.3.4
备注: 永久免费机器
永久免费: 是
检测端口: 22
```

---

## ✏️ 编辑服务器命令

### 编辑名称

```text
编辑名称 1 HK-Oracle
```

### 编辑 IP / 主机

```text
编辑IP 1 1.2.3.4
```

### 编辑备注

```text
编辑备注 1 香港甲骨文主力机
```

### 编辑系统

```text
编辑系统 1 Ubuntu 22.04
```

### 编辑端口

```text
编辑端口 1 443
```

### 编辑价格

```text
编辑价格 1 50 CNY
```

### 编辑周期

```text
编辑周期 1 年付
```

### 编辑到期时间

```text
编辑到期 1 2027-05-01
```

### 设置永久免费

```text
编辑永久 1 是
```

### 取消永久免费

```text
编辑永久 1 否
```

### 设置自动续费

```text
编辑自动续费 1 是
```

### 关闭自动续费

```text
编辑自动续费 1 否
```

---

## 🖥 编辑本机命令

### 编辑本机名称

```text
编辑本机名称 Oracle主控机
```

### 编辑本机备注

```text
编辑本机备注 新加坡主控节点
```

### 编辑本机价格

```text
编辑本机价格 38 CNY
```

### 编辑本机周期

```text
编辑本机周期 年付
```

### 编辑本机到期

```text
编辑本机到期 2027-05-01
```

---

## 🔁 续费命令

### 续费服务器

```text
续费服务器 1 2027-05-01
```

### 本机续费

```text
本机续费 2027-05-01
```

---

## 🎯 告警阈值命令

### 设置 CPU 告警阈值

```text
设置CPU阈值 1 90
```

### 设置内存告警阈值

```text
设置内存阈值 1 85
```

### 设置硬盘告警阈值

```text
设置硬盘阈值 1 80
```

---

## 🌐 Web 面板功能入口

Web 面板支持：

* 📊 服务器总览
* 🖥 所有服务器
* 🔳 卡片视图
* 📋 表格视图
* 🧾 事件记录
* ⚙️ 系统设置
* 🖼 上传背景图
* 🎨 上传主题 ZIP
* 🏷 修改平台名称
* 🌐 上传浏览器标签图标
* 📨 测试 Telegram 推送
* 📡 复制探针部署命令

---

## 📁 GitHub 文件说明

```text
Oracle/
├── bot.py              # Telegram 机器人主程序
├── agent.sh            # 远程服务器探针脚本
├── install.sh          # 机器人安装 / 更新 / 卸载脚本
├── uninstall.sh        # 机器人卸载脚本
├── web_dashboard.py    # Web 面板程序
├── install_web.sh      # Web 面板安装 / 更新脚本
├── uninstall_web.sh    # Web 面板卸载脚本
└── README.md           # 项目说明文档
```

---

## ✅ 推荐更新顺序

如果同时更新机器人、Web 面板和探针，建议顺序：

1. 更新 `bot.py`
2. 更新 `agent.sh`
3. 更新 `web_dashboard.py`
4. 重启机器人
5. 重启 Web 面板
6. 远程服务器重新部署探针

重启命令：

```bash
systemctl restart server-monitor-bot
systemctl restart server-monitor-web
```
