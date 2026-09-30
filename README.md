# 留白 · B 站白名单取关工具

把想保留的 UP 主加入白名单，再预览并逐个取关其余关注。支持 B 站 App 扫码登录、搜索与分页勾选、进度查看、暂停和核对后恢复。

这是面向个人单账号的轻量网页应用，使用 Python 3.12、FastAPI、SQLite 和普通 HTML/CSS/JavaScript。无需 Docker、Node.js 或前端构建。服务默认仅在本机监听，远程部署通过 SSH 通道访问。

> 取关会改变真实账号的关注关系，应用不提供自动撤销。请先核对完整名单。默认串行执行，每次取关请求结束后等待 **5 秒**；这个间隔不保证不会触发 B 站风控。遇到风控会暂停，不自动重试或绕过验证。

## 功能与使用流程

1. **管理员登录**：输入首次启动时生成的管理密码。
2. **扫码登录 B 站**：用 B 站 App 扫描二维码并确认，页面显示当前账号。
3. **加载关注列表**：展示头像、昵称和 UID；支持搜索、分页选择及仅看白名单。白名单按 UID 保存，改名不影响保护。
4. **预览取关**：查看关注总数、保留人数、待取关人数及具体名单。未能完整获取列表时禁止执行。
5. **确认执行**：明确确认后才发送取关请求。页面关闭后后台任务仍会继续；需要停止时点击“暂停”，并等待当前请求结束。
6. **查看结果**：任务结束后重新读取关注关系，区分已确认取关、待核对、失败及结果未确认。

暂停后可以修改白名单，再重新预览剩余名单并确认恢复。恢复前会重新读取完整关注列表，不会将任务开始后新增的关注自动加入原任务。服务重启后任务保持暂停，必须由用户核对并确认恢复。

第一版不支持多账号同时使用、关注分组、白名单导入导出、定时任务或自动重新关注。

## 本地运行

需要 Python **3.12** 和 Git。以下命令适用于 macOS / Linux；Windows 尚未验证。

```sh
git clone https://github.com/aqiuX17/bili-unfollow.git
cd bili-unfollow
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock
BILI_DATA_DIR=./data .venv/bin/uvicorn app:create_app --factory \
  --host 127.0.0.1 --port 22330 --workers 1
```

首次启动会自动创建 `data/`、数据库、加密密钥和随机管理密码。在另一个终端进入项目目录，读取密码：

```sh
cat data/admin-password
```

打开 [http://127.0.0.1:22330/](http://127.0.0.1:22330/)。启动服务不会自动发起取关任务。

必须使用 **单进程（`--workers 1`）**；任务协调依赖单进程状态。`requirements.txt` 固定直接依赖，`requirements.lock` 固定部署时的完整依赖版本，建议使用后者复现环境。

## Linux 服务器部署

以下示例使用具备 sudo 权限的 Ubuntu / Debian 用户。系统需提供 Python 3.12；先用 `python3.12 --version` 确认版本。其他发行版请调整包安装命令。

```sh
sudo apt-get update
sudo apt-get install -y git ca-certificates python3.12 python3.12-venv

git clone https://github.com/aqiuX17/bili-unfollow.git
cd bili-unfollow

sudo useradd --system --user-group --home-dir /opt/bili-unfollow \
  --shell /usr/sbin/nologin bili-unfollow
sudo install -d -m 755 /opt/bili-unfollow
sudo install -m 644 app.py core.py bili.py requirements.txt requirements.lock /opt/bili-unfollow/
sudo cp -R static /opt/bili-unfollow/
sudo python3.12 -m venv /opt/bili-unfollow/venv
sudo /opt/bili-unfollow/venv/bin/pip install -r /opt/bili-unfollow/requirements.lock
sudo install -d -m 700 -o bili-unfollow -g bili-unfollow /var/lib/bili-unfollow
sudo install -m 644 bili-unfollow.service /etc/systemd/system/bili-unfollow.service
sudo systemctl daemon-reload
sudo systemctl enable --now bili-unfollow
sudo systemctl status bili-unfollow --no-pager
sudo cat /var/lib/bili-unfollow/admin-password
```

上面是首次安装步骤；已经存在服务用户时无需重复执行 `useradd`。管理密码文件在服务首次成功启动后生成。

| 项目 | 默认值 |
| --- | --- |
| 程序目录 | `/opt/bili-unfollow` |
| 数据目录 | `/var/lib/bili-unfollow` |
| 服务 / 独立用户 | `bili-unfollow` |
| 监听地址 | `127.0.0.1:22330` |
| 取关请求结束后的等待时间 | 固定 5 秒 |
| 内存上限 | 160 MiB |
| 交换空间上限 | 128 MiB |

systemd 文件还配置了 `MemoryHigh=96M`、文件系统写入限制和失败重启。超限或其他异常引起服务重启时，任务不会自动继续。实际资源占用随名单规模和环境变化；历史部署测量见 [VALIDATION.md](VALIDATION.md)。

### 通过 SSH 访问

在自己的电脑运行，将 `username@your-server` 替换成实际 SSH 用户和主机：

```sh
ssh -N -L 127.0.0.1:22330:127.0.0.1:22330 username@your-server
```

保持该终端开启，然后打开 [http://127.0.0.1:22330/](http://127.0.0.1:22330/)。不需要开放服务器的 22330 端口。

macOS 也可使用 [打开留白.command](打开留白.command)：先在 `~/.ssh/config` 中创建名为 `bili-unfollow-server` 的主机别名并配置密钥登录，再双击脚本。脚本需要免交互的 SSH 密钥认证；也可从终端指定其他 SSH 别名：

```sh
BILI_SSH_HOST=my-server ./打开留白.command
```

应用校验本机 Host / Origin，当前访问方式设计为本机或 SSH 通道；直接换成公网域名或反向代理并不属于已验证的部署方式。

## 执行保护与结果含义

- 先按页读取并按 UID 去重，检查数量与接口总数是否一致，再创建固定执行名单。分页重复、总数变化或读取失败都会使列表失效。
- 后端计算名单，浏览器不能提交任意 UID 作为执行目标。每次发送取关请求前再次检查白名单。
- 白名单变更会使旧预览失效；任务运行或核对时锁定编辑。预览有效期为 10 分钟。
- 同时只允许一个未结束任务；重复点击同一预览不会重复创建任务。
- 登录失效、风控、超时或未知错误都会暂停。结果不确定的请求不会自动再次发送。
- 重启时将中断中的请求标为结果未确认，任务保持暂停；用户重新核对后才能恢复。

“确认已取关”表示在完整列表中核对为已不再关注，不能证明关系一定由本应用改变。接口成功但尚未完成核对时显示“待核对”；超时可能已经在平台生效，因此显示“结果未确认”。读取不完整或无法核对时只展示已知的部分结果，不宣称全部成功。

B 站接口可能变更，也可能限制列表读取。完整性检查依赖平台返回的总数与列表；列表读取期间请避免同时在其他客户端修改关注关系。

## 数据与登录保护

B 站 Cookie 使用 Fernet 加密后保存在 SQLite。密钥与数据位于同一台机器，不能保护已取得该机器 root 权限的攻击者。数据目录权限为 `0700`，凭据文件和数据库为 `0600`。管理会话有效期 24 小时，使用 HttpOnly / SameSite Cookie 和 CSRF 检查。

退出 B 站账号会删除数据库中的当前登录凭据，保留按账号 UID 隔离的白名单。退出前需先暂停并结束未完成任务。数据库备份仍可能包含旧凭据，应按敏感文件保存。

B 站请求使用服务器直连，HTTP 客户端不读取系统代理环境变量。日志不主动记录 Cookie。请勿上传 `data/`、管理密码、密钥、数据库、账号截图或备份；`.gitignore` 已排除常见运行数据。

## 服务维护与备份

```sh
sudo systemctl status bili-unfollow --no-pager
sudo journalctl -u bili-unfollow --no-pager -n 50
sudo systemctl restart bili-unfollow
```

更新程序前先在网页暂停任务，等待当前请求结束；停止服务后更新程序及依赖，再启动。不要替换或删除 `/var/lib/bili-unfollow`，也不要同时运行多个服务实例共享数据库。

备份时先暂停任务并停止服务，再备份整个数据目录；**数据库和加密密钥必须一起保留**：

```sh
sudo systemctl stop bili-unfollow
sudo tar -C /var/lib -czf /root/bili-unfollow-backup.tar.gz bili-unfollow
sudo chmod 600 /root/bili-unfollow-backup.tar.gz
sudo systemctl start bili-unfollow
```

恢复时停止服务，恢复整个数据目录，并保持 `bili-unfollow:bili-unfollow` 所有权、目录 `0700` 和文件 `0600`；再启动服务。恢复后任务不会自动继续。备份路径含登录凭据，不要公开。

### 卸载（保留数据备份）

```sh
sudo systemctl disable --now bili-unfollow
sudo mv /var/lib/bili-unfollow /var/lib/bili-unfollow-backup-$(date +%Y%m%d-%H%M%S)
sudo rm /etc/systemd/system/bili-unfollow.service
sudo systemctl daemon-reload
sudo rm -rf /opt/bili-unfollow
sudo userdel bili-unfollow
```

备份目录会保留，删除备份前请确认不再需要账号数据。

## 测试与项目结构

在已安装依赖的本地环境运行：

```sh
.venv/bin/python -m unittest discover -s tests -v
```

测试使用模拟 B 站接口，不会向真实账号发送取关请求，覆盖白名单保护、分页与改名、列表不完整、预览失效、重复启动、暂停恢复、登录失效、风控、超时和服务重启。验证边界见 [VALIDATION.md](VALIDATION.md)。

| 文件 | 职责 |
| --- | --- |
| `app.py` | 管理认证、扫码登录和 HTTP 接口 |
| `core.py` | SQLite 持久化、预览与任务执行 |
| `bili.py` | B 站请求与错误处理 |
| `static/` | 网页界面，无需构建 |
| `tests/test_safety.py` | 模拟接口测试 |
| `bili-unfollow.service` | systemd 服务与资源限制 |

B 站调用方式参考 [BiliBiliToolPro](https://github.com/RayWangQvQ/BiliBiliToolPro/tree/main/src/Ray.BiliBiliTool.Agent)，未复制其代码。本项目与哔哩哔哩官方无隶属关系，接口适配可能需要随平台变化更新。
