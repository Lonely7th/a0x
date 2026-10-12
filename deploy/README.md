# 单台 Linux 服务器部署（不用 Docker）

服务器直接运行两个 Python 进程：`worker` 采集行情、选股并记录提醒与虚拟成交，`web` 提供网页和同域 `/api/` 接口。Python 虚拟环境安装依赖，systemd 负责开机启动和异常重启。网页使用服务器的 HTTP 80 端口。

服务器会重新创建数据库并采集历史行情，不使用本机数据库。当前页面和 API 没有登录认证。

## 准备服务器

使用 x86_64 Linux 服务器，系统需运行 systemd。先按该发行版的软件包管理方式安装以下组件：

- Python 3.11～3.14，以及对应的 `venv`、`ensurepip` 组件；
- Git；
- 提供 `libgomp.so.1` 的 OpenMP 运行库；
- systemd；部署账号具有 `sudo` 权限，或直接使用 root 账号。

不同 Linux 发行版的软件包名称和安装命令不同，因此这里不使用某一发行版的包管理命令。登录服务器后可以运行以下命令检查：

```bash
python3 --version
python3 -m venv --help
git --version
systemctl --version
```

如果 `python3` 不是符合要求的版本，但服务器上另有合适版本，可在 `setup` 命令前指定，例如 `TA_PYTHON_BIN=/绝对路径/python3.x bash deploy/manage.sh setup`。

部署用户需要有 `sudo` 权限，首次启动时管理脚本会用它安装 systemd 服务文件。云服务商安全组需要允许公网 TCP 80；保留 SSH 端口。可以直接用服务器公网 IP 访问网页。

## 第一次部署

把 `trading_assistant_app` 目录作为一个 Git 仓库上传。当前仓库的拉取命令为：

```bash
git clone https://github.com/Lonely7th/a0x.git trading_assistant_app
cd trading_assistant_app
bash deploy/manage.sh setup
bash deploy/manage.sh initialize
bash deploy/manage.sh start
bash deploy/manage.sh status
```

`setup` 创建 `.venv/` 并安装 `requirements.txt`，同时建立仓库外的 `openai-a-data/` 数据目录。`initialize` 建立空 DuckDB 与 SQLite 数据库，获取证券目录与交易日历，回填至少三年的全市场日线和历史 ST 状态，并修复缺口、审计完整性。免费数据源的速度会影响回填时长，可能需要数小时；请保持 SSH 会话连接。若中断或某批次失败，重新运行 `initialize` 会继续处理未成功的日线批次。审计通过后才能 `start`。

`start` 安装并启动两个 systemd 服务，检查后台心跳。成功后打开 `http://服务器公网IP/`。若无法访问，运行 `bash deploy/manage.sh status` 查看日志，并核对公网 TCP 80 是否放行。

服务器上的实盘记录与虚拟盘交易历史是全新的。服务器正式使用时，请停止原电脑的 `worker`，避免两套系统同时发出提醒。

## 日常操作

在服务器仓库目录执行：

| 目的 | 命令 |
| --- | --- |
| 查看服务状态和最近日志 | `bash deploy/manage.sh status` |
| 停止网页和采集后台 | `bash deploy/manage.sh stop` |
| 再次启动 | `bash deploy/manage.sh start` |
| 备份两个数据库 | `bash deploy/manage.sh backup` |
| 从 GitHub 拉取并更新 | `bash deploy/manage.sh update` |

`backup` 会短暂停止进程，在 `openai-a-data/backups/` 创建一致的备份，再恢复先前运行的服务。`update` 会先备份，再拉取代码、更新 Python 依赖并重启服务；宜选择非交易时段。数据库和 `deploy/.env` 均不在 Git 仓库内，更新源码不会覆盖它们。建议定期把备份转存到服务器以外的位置。

如需调整端口，可编辑 `deploy/.env` 的 `TA_WEB_PORT`，再运行 `bash deploy/manage.sh start`。使用 80 端口时，systemd 只给网页进程开放绑定该端口所需的权限。若以后启用 HTTPS，需要另行配置 443 端口及证书。
