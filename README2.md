# 🤖✨ Telegram Server Monitor Bot ✨🤖

> 漂亮实用的中文 Telegram 服务器监控机器人  
> 支持本机状态、远程服务器探针、在线/离线告警、到期提醒、续费管理、价格备注管理、国家地区识别、国旗显示、CPU/内存/硬盘配置显示、流量/磁盘监控、事件记录、一键部署探针等功能。

<p align="center">
  <b>🖥 Server Monitor ｜ 📡 Agent Probe ｜ 🚨 Alert ｜ ⏰ Expiry Reminder ｜ 🌍 Geo IP ｜ 💰 Renewal Manager</b>
</p>

---

## 📮 联系作者

如需部署帮助、功能定制、问题反馈，请联系作者：

- 👤 Telegram：[@lxfcx6](https://t.me/lxfcx6)
- 💬 联系方式：`@lxfcx6`
- 🧩 项目作者：LXFCX

---

## 🌟 功能特色

### 🖥 本机监控

- ✅ 查看当前主控机器状态
- 🌍 自动识别公网 IP
- 🇺🇸 自动显示国家 / 地区 / 城市 / 国旗
- 🏢 显示运营商 ISP
- 🧬 显示系统版本
- 🧩 显示 CPU 核心数
- 🧠 显示内存使用情况
- 💾 显示磁盘使用情况
- 🌐 显示网络流量统计
- 🏷 支持编辑本机名称
- 📝 支持编辑本机备注
- 💰 支持编辑本机价格
- 📆 支持编辑本机到期时间
- 🔁 支持编辑本机付费周期

---

### 📡 远程服务器探针监控

远程服务器安装探针后，可自动上报真实服务器数据：

- 🟢 在线状态
- 🔴 离线状态
- ⏱️ 系统真实运行时长
- 🕒 系统开机时间
- 🧩 CPU 核心数
- 🧠 内存总量 / 使用量 / 使用率
- 💾 硬盘总量 / 使用量 / 使用率
- 📊 CPU 使用率
- 🌐 累计下载 / 上传流量
- 📡 探针最后上报时间
- 🟢 探针在线 / 🟠 探针超时

效果示例：

🧩 4 Cores ｜ 🧠 23.4GB ｜ 💾 146.6GB
⏱️ 系统运行时长：15 天 3 小时 20 分钟
🕒 系统开机时间：2026-04-23 11:20:03

---

### 🖥 远程服务器管理

- ✅ 添加远程服务器
- 📋 查看服务器列表
- 🖥 查看服务器详情
- 🟢 在线状态检测
- 🔴 离线状态检测
- 🚨 离线告警推送
- ✅ 恢复在线推送
- 🌍 自动识别服务器国家地区
- 🇬🇧 自动显示服务器国旗
- 🏢 自动识别服务器运营商
- 🧬 支持系统信息备注
- 🔌 支持自定义检测端口
- 📝 支持编辑服务器备注
- 🏷 支持编辑服务器名称
- 💰 支持编辑服务器价格
- 💱 支持 CNY / USD / EUR / GBP
- 📆 支持编辑服务器到期时间
- 🔁 支持月付 / 季付 / 年付
- 🎁 支持永久免费的服务器
- 🔁 支持自动续费
- 🗑 支持删除服务器

---

## ⏰ 到期提醒

支持服务器到期时间提醒：

- 📅 提前 30 天提醒
- 📅 提前 14 天提醒
- 📅 提前 7 天提醒
- 📅 提前 3 天提醒
- 📅 提前 1 天提醒
- 🚨 到期当天提醒

自定义提醒天数：

设置到期提醒 60,30,14,7,3,1,0

---

## 🚨 在线 / 离线告警

机器人会定时检测服务器状态：

- 🔴 服务器离线后推送告警
- 🟢 服务器恢复在线后推送通知
- ⏳ 支持离线宽限期，避免网络抖动误报
- 📌 默认离线宽限期：300 秒

---

## 📊 状态面板

支持漂亮的 Telegram 面板展示：

- 📊 服务器总览
- 🖥 本机状态
- 📋 服务器列表
- 🌐 流量使用情况
- 💾 磁盘使用情况
- 🧾 服务器事件
- 🛡 安全状态
- 🔐 登录记录
- 🚫 防爆破状态

---

## 🎛 中文按钮操作

机器人支持中文按钮菜单：

- 📊 服务器总览
- 📋 查看服务器
- 🖥 查看状态
- 🌐 查看流量
- 💾 查看磁盘
- 🧾 添加服务器
- ✏️ 编辑服务器
- 🖥 编辑本机
- 📡 检测服务器
- 🧾 查看事件
- 🛡 安全状态
- 🔐 登录记录
- 🚫 防爆破状态
- 🌍 刷新地区
- ⌨️ 收起键盘

启用中文按钮：

启用命令

---

## 🚀 一键安装

### 方式一：菜单安装

bash <(curl -fsSL https://raw.githubusercontent.com/lxfcx/Oracle/main/install.sh)

运行后会出现菜单：

1) 安装 / 更新机器人
2) 编辑 BOT_TOKEN 和 ADMIN_IDS
0) 卸载机器人并清理所有数据
3) 退出脚本

---

### 方式二：一行命令安装

BOT_TOKEN="你的TG_BOT_TOKEN" ADMIN_IDS="你的TG数字ID" AUTO_INSTALL=1 bash <(curl -fsSL https://raw.githubusercontent.com/lxfcx/Oracle/main/install.sh)

---

## ⚙️ 配置说明

### 🤖 BOT_TOKEN

从 Telegram 官方机器人 [@BotFather](https://t.me/BotFather) 获取。

格式示例：

1234567890:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

---

### 👤 ADMIN_IDS

管理员 Telegram 数字 ID。

多个管理员用英文逗号分隔：

123456789,987654321

---

### 📡 探针上报端口

新版探针默认通过主控机器人接收数据：

端口：8765/tcp

如果服务器开启了防火墙，需要放行：

ufw allow 8765/tcp 2>/dev/null || true
iptables -I INPUT -p tcp --dport 8765 -j ACCEPT 2>/dev/null || true

---

## 📦 安装目录

默认安装到：

/opt/server-monitor-bot

主要文件：

/opt/server-monitor-bot/bot.py
/opt/server-monitor-bot/.env
/opt/server-monitor-bot/servers.db
/opt/server-monitor-bot/venv

---

## 🧹 一键卸载

### 方式一：通过安装脚本菜单卸载

bash <(curl -fsSL https://raw.githubusercontent.com/lxfcx/Oracle/main/install.sh)

选择：

0

然后输入：

YES

---

### 方式二：直接卸载

bash <(curl -fsSL https://raw.githubusercontent.com/lxfcx/Oracle/main/uninstall.sh)

卸载会删除：

/etc/systemd/system/server-monitor-bot.service
/opt/server-monitor-bot
/opt/server-monitor-bot/servers.db
/opt/server-monitor-bot/.env
/opt/server-monitor-bot/venv

⚠️ 不会删除系统公共依赖，例如 `python3`、`curl`、`sqlite3`、`fail2ban`，避免影响服务器其它程序。

---

## 📌 常用命令

### 📊 查看总览

服务器总览

### 📋 查看服务器

查看服务器

### 🖥 查看本机状态

查看状态

### 🌐 查看流量

查看流量

### 💾 查看磁盘

查看磁盘

### 📡 检测服务器

检测服务器

### 🧾 查看事件

查看事件

### 🌍 刷新本机地区

刷新本机地区

### 🌍 刷新全部服务器地区

刷新全部地区

---

## 🧾 添加服务器

发送：

添加服务器

机器人会显示勾选模板。

也可以直接发送：

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

---

## 🎁 添加永久免费服务器

添加服务器
名称: Oracle-Free
主机: 1.2.3.4
备注: 永久免费机器
永久免费: 是
检测端口: 22

永久免费服务器：

- 🎁 不需要价格
- 🎁 不需要到期时间
- 🎁 不触发到期提醒
- 🎁 不需要续费

---

## ✏️ 编辑服务器

### 📝 编辑备注

编辑备注 1 香港甲骨文主力机

### 📆 编辑到期时间

编辑到期 1 2027-05-01

### 💰 编辑价格

编辑价格 1 50 CNY

### 🔁 编辑周期

编辑周期 1 年付

### 🔌 编辑端口

编辑端口 1 443

### 🏷 编辑名称

编辑名称 1 HK-Oracle

### 🧬 编辑系统

编辑系统 1 Ubuntu 22.04

### 🎁 设置永久免费

编辑永久 1 是

取消永久免费：

编辑永久 1 否

### 🔁 设置自动续费

编辑自动续费 1 是

关闭自动续费：

编辑自动续费 1 否

---

## 🖥 编辑本机资料

### 🏷 编辑本机名称

编辑本机名称 Oracle主控机

### 📝 编辑本机备注

编辑本机备注 新加坡主控节点

### 📆 编辑本机到期

编辑本机到期 2027-05-01

### 💰 编辑本机价格

编辑本机价格 38 CNY

### 🔁 编辑本机周期

编辑本机周期 年付

### 🔄 本机续费

本机续费 2027-05-01

---

## 🔁 续费服务器

### 指定日期续费

续费服务器 1 2027-05-01

### 按按钮快速续费

进入服务器详情后，可以点击：

- 📆 月付 +1 月
- 🗓 季付 +3 月
- 📅 年付 +1 年

---

## 📡 一键部署探针

进入服务器详情后点击：

📡 一键部署探针

机器人会生成一条部署命令。

复制到对应服务器 SSH 执行即可。

探针可用于：

- 📡 更准确的实时状态监控
- ⏱️ 自动读取真实系统运行时长
- 🧩 自动读取 CPU 核心数
- 🧠 自动读取内存总量
- 💾 自动读取硬盘总量
- 🌐 远程服务器流量监控
- 💾 远程服务器磁盘监控
- 🚨 异常状态推送
- ✅ 恢复状态推送

---

## 🔍 探针排查命令

### 查看探针状态

systemctl status server-monitor-agent --no-pager -l

### 查看探针日志

journalctl -u server-monitor-agent -n 100 --no-pager

### 重启探针

systemctl restart server-monitor-agent

### 测试主控接收端口

在远程服务器执行：

curl -s http://主控服务器公网IP:8765/health

正常会返回：

{"ok": true, "service": "server-monitor-metrics"}

---

## 🛡 安全相关

### 查看安全状态

安全状态

### 查看登录记录

登录记录

### 查看防爆破状态

防爆破状态

---

## 🔄 节点管理

### 重启 Xray / x-ui / 3x-ui

重启节点

确认执行：

确认重启

---

### 清理系统缓存

清理缓存

确认执行：

确认清理

---

## 🔍 查看服务状态

systemctl status server-monitor-bot --no-pager -l

---

## 📜 查看运行日志

journalctl -u server-monitor-bot -f

查看最近日志：

journalctl -u server-monitor-bot -n 120 --no-pager

---

## 🔄 更新机器人

cd /opt/server-monitor-bot

URL="https://raw.githubusercontent.com/lxfcx/Oracle/main/bot.py"

curl -fL "$URL" -o /tmp/bot.py.new && \
python3 -m py_compile /tmp/bot.py.new && \
mv /tmp/bot.py.new /opt/server-monitor-bot/bot.py && \
chmod +x /opt/server-monitor-bot/bot.py && \
systemctl restart server-monitor-bot && \
systemctl status server-monitor-bot --no-pager -l

---

## ❗️ 常见问题

### 机器人没有反应怎么办？

先查看服务状态：

systemctl status server-monitor-bot --no-pager -l

再查看日志：

journalctl -u server-monitor-bot -n 120 --no-pager

常见原因：

- ❌ BOT_TOKEN 错误
- ❌ ADMIN_IDS 不是你的 Telegram 数字 ID
- ❌ bot.py 没有成功更新
- ❌ Python 语法错误
- ❌ 同一个 Bot Token 被其它程序占用
- ❌ 服务器无法访问 Telegram API

---

### 探针数据显示未知怎么办？

先确认远程服务器探针是否运行：

systemctl status server-monitor-agent --no-pager -l

再确认远程服务器能访问主控端口：

curl -s http://主控服务器公网IP:8765/health

如果无法访问，需要在主控服务器放行端口：

ufw allow 8765/tcp 2>/dev/null || true
iptables -I INPUT -p tcp --dport 8765 -j ACCEPT 2>/dev/null || true

---

### 如何确认 Token 是否正确？

source /opt/server-monitor-bot/.env
curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getMe"

返回 ok:true 才是正常。

---

### 如何确认是否被其它程序占用？

source /opt/server-monitor-bot/.env
curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getUpdates"

如果返回 `Conflict`，说明同一个 Bot Token 正在被其它程序占用。

---

## 🧩 项目文件结构

Oracle/
├── bot.py          # 机器人主程序
├── install.sh      # 一键安装 / 更新 / 配置 / 卸载脚本
├── uninstall.sh    # 一键彻底卸载脚本
├── agent.sh        # 远程服务器探针脚本
└── README.md       # 项目说明

---

## ⚠️ 使用说明

本项目适合个人服务器、VPS、甲骨文云、轻量云、代理节点、业务服务器等监控使用。

请确保：

- ✅ 你拥有服务器管理权限
- ✅ 你了解 Telegram Bot Token 的安全性
- ✅ 不要把 `.env`、Bot Token、管理员 ID 泄露给别人
- ✅ 不要在公开截图里暴露服务器 IP 和 Token

---

## ❤️ 赞助 / 定制 / 反馈

如果你觉得项目好用，欢迎联系作者交流、反馈、定制功能。

- 💬 Telegram：[@lxfcx6](https://t.me/lxfcx6)

---

## 📜 License

本项目仅供学习、个人运维和服务器监控使用。  
使用本项目造成的任何风险，请自行承担。

---

## 🎉 最后

如果部署成功，请在 Telegram 给机器人发送：

启用命令

然后开始使用漂亮的中文按钮菜单。 🚀✨
