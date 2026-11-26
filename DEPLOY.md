# 部署 cpa_guard（给 AI 照做的步骤）

cpa_guard 装在 **CPA 所在的那台服务器**上，常驻后台运行。它会自动处理反重力的"假 429"，并发邮件通知主人。

## AI 必须遵守

1. **按顺序**做下面的步骤，每一步的命令**原样复制**执行，不要自己改写或"优化"。
2. 不要修改 `cpa_guard.py`、`find_trigger.py`、`install.sh`，也不要修改 CPA 的 `config.yaml`。
3. 不要把 `guard.env` 的内容、密码、授权码打印出来或发到聊天里。
4. 不要为了测试而自己往 CPA 发请求，尤其不要故意制造 429（会让谷歌账号有被封的风险）。
5. 只要输出里出现 `[失败]`，就**停下**，把 `[失败]` 那几行原样告诉主人，等主人回答。不要自己想办法绕过。
6. 需要主人提供的信息（第 2 步），直接问主人，不要猜。

## 前提（不满足就停下告诉主人）

- 一台 Linux 服务器，有 systemd，能用 root 登录。
- 这台服务器上已经装好并运行着 CPA（CLIProxyAPI）8.1 或更新版本，里面有反重力（Antigravity）账号。
- CPA 设置了管理密码（`config.yaml` 里 `remote-management` 下面的 `secret-key`）。

## 第 1 步：下载并运行安装脚本

以 root 执行：

```bash
cd /root
rm -rf cpa-antigravity-fake429-guard-main
curl -fsSL -o cpa-guard.tar.gz https://github.com/EmanuellaS826/cpa-antigravity-fake429-guard/archive/refs/heads/main.tar.gz
tar xzf cpa-guard.tar.gz
rm cpa-guard.tar.gz
bash /root/cpa-antigravity-fake429-guard-main/install.sh
```

第一次运行时，脚本会生成配置文件 `/root/cpa-guard/guard.env` 然后停下，显示"已生成配置文件"。这是正常的，接着做第 2 步。

## 第 2 步：问主人要这几样东西

把下面这段话发给主人：

> 安装 cpa_guard 需要你提供：
> 1. CPA 管理面板的登录密码（明文）。
> 2. 用来**发**通知的邮箱地址，以及这个邮箱的 **SMTP 授权码**（不是登录密码。QQ 邮箱：网页版 → 设置 → 账号 → 开启"POP3/SMTP 服务"后生成；163 邮箱：设置 → POP3/SMTP/IMAP；Gmail：Google 账号 → 安全性 → 两步验证 → 应用专用密码）。
> 3. **收**通知的邮箱地址（可以和发件邮箱一样）。

## 第 3 步：填写 /root/cpa-guard/guard.env

打开 `/root/cpa-guard/guard.env`，只改下面这几行，把 `''` 里面填上主人给的值（**保留两边的单引号**）：

| 这一行 | 填什么 | 例子 |
| --- | --- | --- |
| `CPA_MGMT_KEY=''` | CPA 管理面板密码 | `CPA_MGMT_KEY='abc123'` |
| `SMTP_HOST=''` | 发件邮箱的 SMTP 服务器：QQ 填 `smtp.qq.com`，163 填 `smtp.163.com`，Gmail 填 `smtp.gmail.com` | `SMTP_HOST='smtp.qq.com'` |
| `SMTP_USER=''` | 发件邮箱地址 | `SMTP_USER='12345@qq.com'` |
| `SMTP_PASS=''` | 授权码 | `SMTP_PASS='abcdefghijklmnop'` |
| `MAIL_TO=''` | 收件邮箱地址（和发件一样就留空） | `MAIL_TO='me@example.com'` |

`SMTP_PORT='465'` 不用改（Outlook 邮箱才改成 `'587'`）。其他行都保持 `''` 不动。

## 第 4 步：再运行一次安装脚本

```bash
bash /root/cpa-antigravity-fake429-guard-main/install.sh
```

脚本会自己：找到 CPA、检查管理密码、选翻译模型、找日志目录、发一封测试邮件、安装并启动服务。

- **成功**：最后显示 `安装完成。`。
- **失败**：显示 `[失败] ...`。先看下面"常见失败"，表里写了"AI 可以改"的才去改 `guard.env`，改完重新执行第 4 步；其他情况把 `[失败]` 那几行原样告诉主人。

## 第 5 步：让主人确认收到测试邮件

问主人："有没有收到标题是「CPA 守护：测试通知」的邮件？如果收件箱里没有，看一下垃圾箱。"

主人说收到了，部署就完成了。

## 常见失败

| 输出里的 `[失败]` 原因 | 怎么办 |
| --- | --- |
| `CPA_MGMT_KEY 不对` | 告诉主人密码不对，请他再确认一次管理面板的登录密码；拿到后 AI 可以改 `CPA_MGMT_KEY`，再执行第 4 步 |
| `邮件通知失败：SMTPAuthenticationError ...` | 授权码不对，或者邮箱没开 SMTP 服务。告诉主人，拿到新授权码后 AI 可以改 `SMTP_PASS` |
| `邮件通知失败：TimeoutError` 或 `ConnectionRefusedError` 或 `OSError` | 服务器商可能封了这个端口。AI 可以把 `SMTP_PORT` 改成 `'587'` 试一次；还不行就告诉主人 |
| `找不到 CPA 的 config.yaml` / `找到好几个像 CPA 的 config.yaml` | 问主人 CPA 的 `config.yaml` 在哪个目录，AI 可以把目录填进 `CPA_DIR`（例如 `CPA_DIR='/root/cliproxyapi'`） |
| `找不到 CPA 的日志目录` | 问主人 CPA 的日志目录（里面有 `main.log`），AI 可以填进 `LOG_DIR` |
| `CPA 里没有默认的翻译模型` | 把这行列出的模型名告诉主人，请他选一个，AI 可以填进 `REWRITE_MODEL` |
| `连不上 CPA` | CPA 没在运行或地址不对。告诉主人 |
| `没有 oauth → providers → antigravity 这一段` | 需要改 CPA 的 `config.yaml`，告诉主人（照仓库 `README.md` 第 3 节加） |
| `已经用旧办法……装过 cpa_guard` | 告诉主人，等他决定 |
| 其他 | 原样告诉主人 |

脚本输出里的 `[提醒]` 不是失败，可以继续，但要转告主人。

## 以后升级

重新执行第 1 步的全部命令就是升级（`guard.env` 会保留，不用重填），最后显示 `安装完成。` 就好了。

## 常用命令

```bash
journalctl -u cpa-guard -f                 # 实时看运行日志（Ctrl+C 退出）
systemctl status cpa-guard                 # 看服务是不是在运行
cat /root/cpa-guard/state/events.log       # 历史处理记录
```

收到"已停止所有测试请求（人工 --resume 才会恢复）"的邮件时，说明谷歌账号可能被封（403）。**不要**自己恢复，交给主人处理。

## 卸载

```bash
systemctl disable --now cpa-guard
rm /etc/systemd/system/cpa-guard.service
systemctl daemon-reload
rm -rf /root/cpa-guard /root/cpa-antigravity-fake429-guard-main
```
