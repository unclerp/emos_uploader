# qBittorrent 下载完成后自动上传

监控每 5 秒轮询 qB 原生 Web API。先确认下载器原生状态，再读取所选视频的文件列表；不使用额外的固定等待时间或连续文件大小稳定次数。

## 原生状态与文件就绪

- `uploading`、`stalledUP`、`forcedUP`、`queuedUP`：允许检查文件。
- qB 5.x 的 `stoppedUP`：允许检查文件；兼容 qB 4.x 的 `pausedUP`。
- `checkingUP`、`checkingDL`、`checkingResumeData`、`moving`、所有下载状态、`error`、`missingFiles` 和未知状态：等待下轮。
- 同时要求种子的 `progress == 1`、`amount_left == 0`。
- `/torrents/files` 中只处理 `priority > 0`、`progress == 1` 的受支持视频。未选择下载的文件不参与上传。
- `save_path + name` 按最长完整目录前缀映射一次。本下载器未配置映射时使用全局监控映射。文件名保留大小写和字面百分号，不进行 URL 解码。
- 最终本地文件必须存在、可读，且大小与 qB 的 `size` 一致。qB 返回逻辑文件名时，如果磁盘仍是 `.!qB` 临时文件，就等待后续扫描，不剥后缀或记成已完成。
- 文件出队开始处理前重新查询 qB；如果此时进入校验、移动等状态，保留待处理任务，恢复就绪后重新入队。

Tracker 的连接错误与种子的 `state == error` 不同。监控不以 tracker 状态决定能否上传；已下载完成的视频不会因为 tracker 报错而被阻止。

连接成功后日志记录 `/app/version` 实际返回的版本。本次核对 hd-ca 宿主机二进制和 10496 实例的 `/app/version` 均为 **v5.1.4**，`/app/webapiVersion` 为 **2.11.4**，实际返回已观察到 `stoppedUP`、`stalledUP`、`uploading`、`stoppedDL`、`stalledDL`、`downloading`，其状态字符串以[对应发布版序列化源码](https://github.com/qbittorrent/qBittorrent/blob/release-5.1.4/src/webui/api/serialize/serialize_torrent.cpp)为准；文件字段见[文件列表接口源码](https://github.com/qbittorrent/qBittorrent/blob/release-5.1.4/src/webui/api/torrentscontroller.cpp)。

## 接收、去重与重试

同步完成数据库任务创建和实际入队后才返回已接收。接收失败、接口暂时不可用、路径未就绪都不会永久屏蔽种子。每个种子独立检查，单个接口错误不影响其他种子。

同一路径、文件大小与修改时间、上传目标组合使用同一持久任务；正在排队或处理中的任务不重复入队，已经成功的任务在重启后也不会再次上传。相同路径的文件内容版本或目标变化会建立独立任务。

沿用原有失败策略：网络/API/上传失败允许自动重试，两次失败处理之间至少间隔 60 秒；未匹配到影视条目等失败等待手动处理。自动重试标记存入数据库，重启不会丢失。重启中断的任务在 qB 再次就绪后接续；失败历史仍保留，最终成功后显示已解决。

修改不改变上传后的删除或移动行为，也不会自动添加或启用下载器。配置 qB 监控前仍需正确填写 WebUI 连接及容器路径映射。
